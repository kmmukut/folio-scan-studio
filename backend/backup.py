"""Backups of everything Folio keeps, and restoring one.

A backup is one .tar.gz file:

  folio-backup.json          what it is: Folio version, edition, date, contents
  data/access.sqlite3        accounts (password hashes; sign-ins are left out)
  data/print-log.sqlite3     print statistics
  data/devices.json          scanners, printer settings, print servers
  data/cups-admin.json       administrator logins for other CUPS servers
  data/jobs/…                documents: original scans and edits (not the preview cache)
  printers/<name>.json/.ppd  the built-in print server's printers, with their drivers

Restoring replaces Folio's data with the backup's. The data it replaces is
moved to data/.before-restore first, so a mistaken restore can be undone by
hand. Built-in printers missing from the print server are recreated from the
backup's drivers, so they work again without being found and set up anew.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path, PurePosixPath

from backend import ipp

FORMAT = 1
MANIFEST = 'folio-backup.json'
DATABASES = ('access.sqlite3', 'print-log.sqlite3')
FILES = ('devices.json', 'cups-admin.json')
PRINTER_ATTRIBUTES = ['printer-name', 'device-uri', 'printer-info', 'printer-location', 'printer-is-shared']
REPLACED = (*DATABASES, *FILES, 'jobs', 'managed-airscan.conf')  # What a restore swaps out


class BackupError(ValueError):
    """The file is not a Folio backup this version can restore."""


def _version(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.split('-')[0].split('.')[:3])
    except ValueError:
        raise BackupError('The backup names an unknown Folio version') from None


def _sqlite_copy(source: Path, target: Path, *, drop_sessions: bool = False) -> None:
    """A consistent copy of a database that may be in use."""
    src, dst = sqlite3.connect(source, timeout=10), sqlite3.connect(target)
    try:
        src.backup(dst)
        if drop_sessions:
            dst.execute('DELETE FROM sessions')  # Sign-ins don't belong in a backup
            dst.commit()
    finally:
        src.close()
        dst.close()


def _documents(data_dir: Path) -> list[Path]:
    jobs = data_dir / 'jobs'
    if not jobs.is_dir():
        return []
    return sorted(path for path in jobs.rglob('*') if path.is_file() and 'cache' not in path.relative_to(jobs).parts)


def builtin_printers(conn: ipp.Connection) -> list[tuple[dict, bytes]]:
    """Each printer on the built-in server with its driver (PPD), if it has one."""
    try:
        response = conn.request(ipp.CUPS_GET_PRINTERS, [(ipp.KEYWORD, 'requested-attributes', PRINTER_ATTRIBUTES)],
                                authenticate=False, timeout=15)
    except ipp.IPPError as exc:
        if exc.status == 0x0406:  # not-found: no printers set up yet
            return []
        raise
    found = []
    for attrs in response.all(ipp.PRINTER):
        first = lambda key, default='': (attrs.get(key) or [default])[0]
        printer = {'name': first('printer-name'), 'device_uri': first('device-uri'), 'info': first('printer-info'),
                   'location': first('printer-location'), 'shared': bool(first('printer-is-shared', True))}
        try:
            ppd = conn.request(ipp.CUPS_GET_PPD, [(ipp.URI, 'printer-uri', conn.printer_uri(printer['name']))],
                               authenticate=False, timeout=15).data
        except ipp.IPPError:
            ppd = b''  # A queue without a driver file (e.g. a raw queue)
        found.append((printer, ppd))
    return found


def create(data_dir: Path, target: Path, *, version: str, edition: str,
           printers: list[tuple[dict, bytes]] = (), progress=None) -> dict:
    """Write a backup to ``target``; returns its manifest."""
    documents = _documents(data_dir)
    manifest = {'format': FORMAT, 'folio_version': version, 'edition': edition,
                'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'documents': len({p.relative_to(data_dir / 'jobs').parts[0] for p in documents}),
                'printers': [p['name'] for p, _ in printers]}
    scratch = target.parent / '.backup-scratch'
    scratch.mkdir(exist_ok=True)
    try:
        with tarfile.open(target, 'w:gz', compresslevel=6) as tar:
            def add_bytes(name: str, content: bytes) -> None:
                info = tarfile.TarInfo(name)
                info.size, info.mtime, info.mode = len(content), int(time.time()), 0o600
                tar.addfile(info, io.BytesIO(content))
            add_bytes(MANIFEST, json.dumps(manifest, indent=2).encode())
            for name in DATABASES:
                if (data_dir / name).exists():
                    copy = scratch / name
                    _sqlite_copy(data_dir / name, copy, drop_sessions=name == 'access.sqlite3')
                    tar.add(copy, f'data/{name}')
            for name in FILES:
                if (data_dir / name).exists():
                    tar.add(data_dir / name, f'data/{name}')
            for index, path in enumerate(documents):
                tar.add(path, f'data/{path.relative_to(data_dir).as_posix()}')
                if progress and index % 25 == 0:
                    progress(index, len(documents))
            for printer, ppd in printers:
                add_bytes(f"printers/{printer['name']}.json", json.dumps(printer, indent=2).encode())
                if ppd:
                    add_bytes(f"printers/{printer['name']}.ppd", ppd)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return manifest


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (not path.is_absolute() and '..' not in path.parts and bool(path.parts)
            and (name == MANIFEST or path.parts[0] in ('data', 'printers')))


def inspect(archive: Path, current_version: str) -> dict:
    """Check that ``archive`` is a backup this Folio can restore; returns its manifest."""
    try:
        with tarfile.open(archive, 'r:gz') as tar:
            members = tar.getmembers()
            for member in members:
                if not _safe_name(member.name) or not (member.isfile() or member.isdir()):
                    raise BackupError(f'Unexpected item in the backup: {member.name[:80]}')
            try:
                manifest = json.loads(tar.extractfile(MANIFEST).read())
            except (KeyError, AttributeError, ValueError):
                raise BackupError('This is not a Folio backup (it has no folio-backup.json)') from None
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError('This file is not a readable .tar.gz backup') from exc
    if manifest.get('format') != FORMAT:
        raise BackupError('This backup was made in a format this Folio does not know')
    if _version(manifest.get('folio_version', '0')) > _version(current_version):
        raise BackupError(f"This backup was made by Folio {manifest['folio_version']}, which is newer than this "
                          f'one ({current_version}). Upgrade Folio first.')
    return manifest


def restore(archive: Path, data_dir: Path, *, current_version: str, conn: ipp.Connection | None,
            progress=None, on_swapped=None) -> dict:
    """Replace Folio's data with the backup's and recreate missing built-in printers.

    ``on_swapped`` runs as soon as the backup's data is in place (before the
    printers are recreated), so Folio can open the restored databases at once.
    """
    manifest = inspect(archive, current_version)
    staging, aside = data_dir / '.restore-staging', data_dir / '.before-restore'
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        with tarfile.open(archive, 'r:gz') as tar:
            members = [m for m in tar.getmembers() if m.isfile() and m.name != MANIFEST]
            for index, member in enumerate(members):
                target = staging / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, open(target, 'wb') as out:
                    shutil.copyfileobj(source, out)
                if progress and index % 25 == 0:
                    progress(index, len(members))
        # Swap item by item: the current one goes aside, the backup's takes its
        # place at once, so nothing is missing for longer than a rename.
        shutil.rmtree(aside, ignore_errors=True)
        aside.mkdir()
        for name in REPLACED:
            for path in (data_dir / name, data_dir / f'{name}-wal', data_dir / f'{name}-shm'):
                if path.exists():
                    shutil.move(str(path), aside / path.name)
            if (staging / 'data' / name).exists():
                shutil.move(str(staging / 'data' / name), data_dir / name)
        if on_swapped:
            on_swapped()
        restored, failed = _recreate_printers(staging / 'printers', conn)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {'manifest': manifest, 'printers_restored': restored, 'printers_failed': failed,
            'previous_data': aside.name}


def _recreate_printers(folder: Path, conn: ipp.Connection | None) -> tuple[list[str], dict[str, str]]:
    restored, failed = [], {}
    if conn is None or not folder.is_dir():
        return restored, failed
    try:
        existing = {p['name'] for p, _ in builtin_printers(conn)}
    except (ipp.IPPError, OSError) as exc:
        # The data is already restored; say which printers still need setting up.
        return restored, {path.stem: f'the print server did not answer ({exc})' for path in folder.glob('*.json')}
    for description in sorted(folder.glob('*.json')):
        printer = json.loads(description.read_text(encoding='utf-8'))
        name = str(printer.get('name', ''))
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', name) or name in existing:
            continue
        ppd = folder / f'{name}.ppd'
        if not ppd.exists():
            failed[name] = 'the backup has no driver for it; add it again in Devices'
            continue
        try:
            conn.request(ipp.CUPS_ADD_MODIFY_PRINTER, [(ipp.URI, 'printer-uri', conn.printer_uri(name))],
                         resource='/admin/', timeout=60, data=ppd.read_bytes(), printer_attributes=[
                             (ipp.URI, 'device-uri', printer.get('device_uri') or 'file:///dev/null'),
                             (ipp.TEXT, 'printer-info', (printer.get('info') or name)[:127]),
                             (ipp.TEXT, 'printer-location', (printer.get('location') or '')[:127]),
                             (ipp.BOOLEAN, 'printer-is-shared', bool(printer.get('shared', True))),
                             (ipp.BOOLEAN, 'printer-is-accepting-jobs', True),
                             (ipp.ENUM, 'printer-state', 3)])
            restored.append(name)
        except ipp.IPPError as exc:
            failed[name] = str(exc)[:160]
    return restored, failed
