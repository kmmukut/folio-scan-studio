"""Printer drivers people add themselves, from a folder mounted read-only at
/drivers (FOLIO_DRIVERS_DIR on the host; see compose.yaml).

Some printers print well only with their maker's own driver, which Folio
can't ship (licences, CPU types). So at every start, before the built-in
print server starts, the container installs what it finds in that folder,
as root (``python -m backend.drivers``):

- Debian packages (.deb) are installed with dpkg, their setup scripts
  included, as on any Debian system. Dependencies the image lacks don't stop
  them; they show in System check (put the packages they name in the folder
  too). Packages for this computer's kind come first; 32-bit x86 ones are
  accepted on 64-bit x86.
- RPM packages (.rpm) are unpacked; their scripts don't run, and they never
  replace the image's own files.
- Archives (.tar.gz, .tgz, .tar.xz, .tar.bz2, .zip) are unpacked into
  /opt/folio-drivers. Packages inside them are installed as above (Debian
  ones in preference to RPM); Samsung's and HP's Unified Linux Driver is set
  up the way its own installer does it; anything else is used where it lies.
- PPD files (.ppd, .ppd.gz) are drivers on their own (PostScript printers).
- A folder named root/ is copied onto the system as it is, for anything
  else (but never over Folio itself or its data).

Then every driver (PPD) that came with them is made visible to CUPS, the
filter programs each one names are linked where CUPS looks for them, library
folders are made known to the system, and each program is checked: made for
this computer's CPU, with the libraries and interpreter it needs. Folio
reads the result (System check) and lists these drivers first.

Whatever is in the folder runs as root, so it is used only when mounted
read-only: Folio itself, an ordinary user, must not be able to change it.
Links and unpacked archives are rebuilt at every start; packages and copied
files stay until the container is recreated (an update, or
``docker compose up -d --force-recreate``).
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath

DRIVERS_DIR = Path('/drivers')
STATUS_FILE = Path(os.environ.get('FOLIO_RUN_DIR', '/run/folio')) / 'drivers.json'
# Inside the system root (``root`` below), as the tests install into a folder.
UNPACKED = 'opt/folio-drivers'                     # Archives, unpacked afresh at every start
LINKED_PPDS = 'usr/share/cups/model/folio-drivers'  # Drivers lying where CUPS doesn't look
CUPS_MODEL = 'usr/share/cups/model'                 # Where CUPS looks: ppd-name "x/y.ppd"…
LSB_PPDS = 'usr/share/ppd'                          # …and here: "lsb/usr/x/y.ppd"
FILTERS = 'usr/lib/cups/filter'
LD_CONF = 'etc/ld.so.conf.d/folio-drivers.conf'
LINKS_FILE = 'var/lib/folio-drivers/links.json'    # Filter links made last time, removed first
ARCHIVES = ('.tar.gz', '.tgz', '.tar.xz', '.txz', '.tar.bz2', '.tbz2', '.tar', '.zip')
PROTECTED = {'app', 'data', 'drivers', 'proc', 'sys', 'dev', 'run', 'tmp'}  # root/ never writes there
DPKG_SECONDS = 600
ELF_MACHINES = {3: 'x86 (32-bit)', 40: 'ARM (32-bit)', 62: 'x86-64', 183: 'ARM (64-bit)'}
MACHINES = {'x86_64': (62, 64, 'amd64'), 'amd64': (62, 64, 'amd64'), 'i686': (3, 32, 'i386'),
            'i386': (3, 32, 'i386'), 'aarch64': (183, 64, 'arm64'), 'arm64': (183, 64, 'arm64'),
            'armv7l': (40, 32, 'armhf'), 'armv6l': (40, 32, 'armhf')}
LIBRARY_DIRS = {62: ['lib/x86_64-linux-gnu', 'usr/lib/x86_64-linux-gnu', 'lib64', 'usr/lib64'],
                3: ['lib/i386-linux-gnu', 'usr/lib/i386-linux-gnu', 'lib32', 'usr/lib32'],
                183: ['lib/aarch64-linux-gnu', 'usr/lib/aarch64-linux-gnu', 'lib64', 'usr/lib64'],
                40: ['lib/arm-linux-gnueabihf', 'usr/lib/arm-linux-gnueabihf']}


class DriverError(Exception):
    """A file in the drivers folder that can't be used, said plainly."""


# --- What the folder holds -----------------------------------------------------------

def install(folder: Path = DRIVERS_DIR, root: Path = Path('/'), *, machine: str | None = None,
            read_only_required: bool = True) -> dict:
    """Install what ``folder`` holds into the system at ``root``; returns the report."""
    machine = machine or os.uname().machine
    report = {'state': 'none', 'folder': str(folder), 'machine': machine, 'sources': [], 'drivers': [],
              'problems': [], 'checked_at': time.time()}
    undo_last_start(root)
    entries = sorted(e for e in folder.iterdir() if not e.name.startswith('.')) if folder.is_dir() else []
    if not entries:
        return report
    if read_only_required and not os.statvfs(folder).f_flag & os.ST_RDONLY:
        report.update(state='refused', message=(
            'The drivers folder is not mounted read-only, so it was not used. What is in it runs as root, so '
            'mount it with ":ro" (see compose.yaml).'))
        return report
    report['state'] = 'done'
    added: list[tuple[str, Path]] = []  # (where it came from, a file now on the system)
    packages: list[tuple[str, Path]] = []

    def done(name: str, kind: str, result: str, detail: str = '') -> None:
        report['sources'].append({'name': name, 'kind': kind, 'result': result, 'detail': detail})

    for entry in entries:
        name, lower = entry.name, entry.name.lower()
        try:
            if entry.is_dir():
                if name != 'root':
                    done(name, 'folder', 'skipped', 'Only a folder named "root" is used (copied onto the system).')
                    continue
                files, refused = place(entry, root, replace=True)
                added += [(name, f) for f in files]
                done(name, 'files', 'copied' if not refused else 'partly',
                     f"{len(files)} file(s)" + (f"; not copied (Folio's own): {', '.join(refused[:5])}" if refused else ''))
            elif lower.endswith('.deb'):
                packages.append((name, entry))
            elif lower.endswith('.rpm'):
                files = unpack_rpm(entry, root)
                added += [(name, f) for f in files]
                done(name, 'package', 'unpacked', f'{len(files)} file(s)')
            elif lower.endswith(ARCHIVES):
                kind, files, inner = unpack_archive(entry, root, machine)
                added += [(name, f) for f in files]
                packages += [(f'{name}: {p.name}', p) for p in inner]
                done(name, 'archive', 'unpacked', kind)
            elif lower.endswith(('.ppd', '.ppd.gz')):
                target = root / LINKED_PPDS / 'ppd' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(entry, target)
                added.append((name, target))
                done(name, 'driver', 'added')
            else:
                done(name, 'file', 'skipped', 'Not a kind of driver Folio installs (.deb, .rpm, an archive, '
                                              'a .ppd, or a folder named "root").')
        except (DriverError, OSError, ValueError, subprocess.SubprocessError, tarfile.TarError,
                zipfile.BadZipFile) as exc:
            done(name, 'file', 'failed', str(exc) or type(exc).__name__)
    if packages:
        try:
            for name, kind, result, detail, files in install_debs(packages, root, machine):
                done(name, kind, result, detail)
                added += [(name, f) for f in files]
        except (OSError, subprocess.SubprocessError) as exc:
            for name, _ in packages:
                done(name, 'package', 'failed', str(exc) or type(exc).__name__)
    make_known(added, root, machine, report)
    return report


def undo_last_start(root: Path) -> None:
    """Links and unpacked archives are rebuilt at every start, so a driver
    taken out of the folder doesn't linger."""
    try:
        for link in json.loads((root / LINKS_FILE).read_text()):
            path = root / link
            if path.is_symlink():
                path.unlink()
    except (OSError, ValueError, TypeError):
        pass
    for folder in (UNPACKED, LINKED_PPDS):
        shutil.rmtree(root / folder, ignore_errors=True)
    (root / LD_CONF).unlink(missing_ok=True)


def slug(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-.')[:80] or 'driver'


def place(source: Path, root: Path, *, replace: bool) -> tuple[list[Path], list[str]]:
    """Copy a tree onto the system; never into Folio itself or its data.
    Returns the files copied and the paths refused."""
    files, refused = [], []
    for path in sorted(source.rglob('*')):
        rel = path.relative_to(source)
        if rel.parts[0] in PROTECTED:
            if rel.parts[0] not in refused:
                refused.append(rel.parts[0])
            continue
        target = root / rel
        if path.is_dir() and not path.is_symlink():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists() or target.is_symlink():
            if not replace or target.is_dir():
                continue
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            target.symlink_to(os.readlink(path))
        else:
            shutil.copyfile(path, target)
            target.chmod(path.stat().st_mode & 0o755)  # Root's alone to change
        files.append(target)
    return files, refused


# --- Packages and archives ----------------------------------------------------------------

def unpack_rpm(path: Path, root: Path) -> list[Path]:
    """An RPM's files, without its scripts, never over the image's own."""
    stage = root / UNPACKED / f'rpm-{slug(path.name)}'
    stage.mkdir(parents=True, exist_ok=True)
    bsdtar = shutil.which('bsdtar')
    if not bsdtar:
        raise DriverError('RPM packages need bsdtar, which this image lacks')
    done = subprocess.run([bsdtar, '-x', '--no-same-owner', '--no-same-permissions', '-f', str(path),
                           '-C', str(stage)], capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        raise DriverError(f"Couldn't unpack it: {(done.stderr.strip().splitlines() or ['bsdtar failed'])[-1]}")
    files, _ = place(stage, root, replace=False)
    shutil.rmtree(stage, ignore_errors=True)
    return files


def unpack_archive(path: Path, root: Path, machine: str) -> tuple[str, list[Path], list[Path]]:
    """Unpack an archive; returns what it turned out to be, the files to use,
    and the Debian packages inside it to install."""
    stage = root / UNPACKED / slug(path.name)
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    if path.name.lower().endswith('.zip'):
        unzip(path, stage)
    else:
        with tarfile.open(path) as archive:
            archive.extractall(stage, filter='data')  # Refuses absolute paths, "..", and links out of it
    uld = next((p.parent.parent for p in stage.glob('*/noarch/oem.conf')), None) or \
        (stage if (stage / 'noarch' / 'oem.conf').exists() else None)
    if uld is not None:
        files = setup_uld(uld, root, machine)
        shutil.rmtree(stage, ignore_errors=True)
        return "the maker's Unified Linux Driver", files, []
    debs = choose_debs(sorted(stage.rglob('*.deb')), machine)
    if debs:
        return f'{len(debs)} Debian package(s) inside', [], debs
    rpms = [p for p in sorted(stage.rglob('*.rpm')) if rpm_fits(p.name, machine)]
    if rpms:
        return f'{len(rpms)} RPM package(s) inside', [f for p in rpms for f in unpack_rpm(p, root)], []
    return 'files', [p for p in stage.rglob('*') if p.is_file() or p.is_symlink()], []


def unzip(path: Path, stage: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = PurePosixPath(info.filename)
            if name.is_absolute() or '..' in name.parts:
                raise DriverError(f'Unsafe path in the archive: {info.filename}')
            target = stage.joinpath(*name.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as data, open(target, 'wb') as out:
                shutil.copyfileobj(data, out)
            mode = (info.external_attr >> 16) & 0o755
            target.chmod(mode or 0o644)


def rpm_fits(name: str, machine: str) -> bool:
    arch = name.lower().rsplit('.', 2)[-2] if name.count('.') >= 2 else ''
    wanted = {'x86_64': {'x86_64'}, 'aarch64': {'aarch64'}, 'armv7l': {'armv7hl', 'armhfp'},
              'i686': {'i386', 'i586', 'i686'}}.get(machine, {machine})
    return arch in wanted | {'noarch'}


def setup_uld(base: Path, root: Path, machine: str) -> list[Path]:
    """Samsung's and HP's Unified Linux Driver, set up as its installer does:
    its filters and colour engine in /opt/smfp-common/printer, its drivers
    (with the per-model tuning they look for) as CUPS's uld-<maker>."""
    vendor = re.search(r'^VENDOR=(\w+)', (base / 'noarch' / 'oem.conf').read_text(errors='replace'), re.M)
    vendor = (vendor.group(1) if vendor else 'samsung').lower()
    arch = {'x86_64': 'x86_64', 'i686': 'i386', 'i386': 'i386'}.get(machine)
    if not arch or not (base / arch).is_dir():
        raise DriverError(f"{vendor.capitalize()}'s Unified Linux Driver exists for x86 computers only, "
                          f'not this one ({machine})')
    printer = root / 'opt' / 'smfp-common' / 'printer'
    files = []
    for name, folder, mode in (('libscmssc.so', 'lib', 0o644), ('rastertospl', 'bin', 0o755),
                               ('pstosecps', 'bin', 0o755)):
        if (base / arch / name).is_file():
            target = printer / folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(base / arch / name, target)
            target.chmod(mode)
            files.append(target)
    model = root / CUPS_MODEL / f'uld-{slug(vendor)}'
    shutil.rmtree(model, ignore_errors=True)
    shutil.copytree(base / 'noarch' / 'share' / 'ppd', model)
    return files + [p for p in model.rglob('*') if p.is_file()]


def deb_fields(path: Path) -> dict:
    done = subprocess.run(['dpkg-deb', '-f', str(path), 'Package', 'Architecture'], capture_output=True,
                          text=True, timeout=60)
    if done.returncode != 0:
        raise DriverError(f'Not a Debian package dpkg can read: {done.stderr.strip()[-200:]}')
    return dict(line.split(': ', 1) for line in done.stdout.splitlines() if ': ' in line)


def accepts(arch: str, machine: str) -> bool:
    native = MACHINES.get(machine, (0, 0, machine))[2]
    return arch in (native, 'all') or (native == 'amd64' and arch == 'i386')


def choose_debs(paths: list[Path], machine: str) -> list[Path]:
    """One package of each name: this computer's kind, else 'all', else 32-bit x86 on x86-64."""
    native = MACHINES.get(machine, (0, 0, machine))[2]
    best: dict[str, tuple[int, Path]] = {}
    for path in paths:
        try:
            fields = deb_fields(path)
        except (DriverError, OSError, subprocess.SubprocessError):
            continue
        arch = fields.get('Architecture', '')
        if not accepts(arch, machine):
            continue
        rank = 2 if arch == native else 1 if arch == 'all' else 0
        name = fields.get('Package', path.name)
        if name not in best or rank > best[name][0]:
            best[name] = (rank, path)
    return [path for _, path in best.values()]


def deb_files(path: Path) -> list[str]:
    done = subprocess.run(['dpkg-deb', '-c', str(path)], capture_output=True, text=True, timeout=120)
    files = []
    for line in done.stdout.splitlines():
        if not line or line[0] == 'd':
            continue
        entry = line.split(None, 5)[-1].split(' -> ')[0]
        files.append(entry[2:] if entry.startswith('./') else entry.lstrip('/'))
    return files


def install_debs(packages: list[tuple[str, Path]], root: Path, machine: str) -> list[tuple]:
    """Install Debian packages together (so they can depend on each other).
    Returns (name, kind, result, detail, files) for each."""
    results, chosen = [], []
    for name, path in packages:
        try:
            fields = deb_fields(path)
        except (DriverError, OSError, subprocess.SubprocessError) as exc:
            results.append((name, 'package', 'failed', str(exc), []))
            continue
        arch = fields.get('Architecture', '')
        if not accepts(arch, machine):
            results.append((name, 'package', 'skipped', f'Made for {arch} computers, not this one ({machine})', []))
            continue
        chosen.append((name, path, fields.get('Package', name), [root / f for f in deb_files(path)]))
    if not chosen:
        return results
    if root == Path('/'):
        done = subprocess.run(['dpkg', '-i', '--force-depends', '--force-architecture', '--force-confdef',
                               '--force-confold', *[str(p) for _, p, _, _ in chosen]],
                              capture_output=True, text=True, timeout=DPKG_SECONDS, stdin=subprocess.DEVNULL,
                              env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'})
        said = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
        for name, _, package, files in chosen:
            state = subprocess.run(['dpkg-query', '-W', '-f=${db:Status-Abbrev}', package],
                                   capture_output=True, text=True).stdout.strip()
            mine = [line.strip() for line in said if package in line and ('error' in line.lower() or
                                                                          'depends' in line.lower())]
            if state == 'ii':
                results.append((name, 'package', 'installed', '; '.join(mine[:2]), files))
            elif state.startswith('i') or state.startswith('h'):
                results.append((name, 'package', 'partly',
                                'Installed, but its setup script failed' + (f": {'; '.join(mine[:2])}" if mine else ''),
                                files))
            else:
                results.append((name, 'package', 'failed', '; '.join(mine[:3] or said[-3:]), []))
    else:  # Installing into a folder (the tests): its files only
        for name, path, _, files in chosen:
            subprocess.run(['dpkg-deb', '-x', str(path), str(root)], check=True, timeout=120)
            results.append((name, 'package', 'installed', '', files))
    return results


# --- Making drivers known to CUPS, and checking their programs ----------------------

def make_known(added: list[tuple[str, Path]], root: Path, machine: str, report: dict) -> None:
    by_name: dict[str, list[Path]] = {}
    for _, path in added:
        by_name.setdefault(path.name, []).append(path)
    links: list[str] = []
    libraries = sorted({str(p.parent.relative_to(root)) for _, p in added
                        if re.search(r'\.so(\.\d+)*$', p.name) and not under(p, root, *standard_dirs(machine))})
    if libraries:
        (root / LD_CONF).parent.mkdir(parents=True, exist_ok=True)
        (root / LD_CONF).write_text(''.join(f'/{d}\n' for d in libraries))
        if root == Path('/') and shutil.which('ldconfig'):
            subprocess.run(['ldconfig'], capture_output=True, timeout=120)
    checked: dict[str, list[str]] = {}
    seen = set()
    for source, path in added:
        if not path.name.lower().endswith(('.ppd', '.ppd.gz')) or path in seen or not path.is_file():
            continue
        seen.add(path)
        text = ppd_text(path)
        if text is None:
            continue
        ppd = ppd_name(path, source, root)
        nickname = re.search(r'^\*NickName:\s*"([^"]*)"', text, re.M)
        problems = []
        for program in ppd_programs(text):
            if program not in checked:
                checked[program] = link_program(program, by_name, root, machine, links)
            problems += checked[program]
        report['drivers'].append({'ppd': ppd, 'name': nickname.group(1).strip() if nickname else path.name,
                                  'source': source, 'problems': problems})
    report['problems'] = sorted({p for d in report['drivers'] for p in d['problems']})
    (root / LINKS_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / LINKS_FILE).write_text(json.dumps(links))


def under(path: Path, root: Path, *folders: str) -> bool:
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else ''
    return any(rel == f or rel.startswith(f + '/') for f in folders)


def ppd_text(path: Path) -> str | None:
    try:
        data = gzip.decompress(path.read_bytes()) if path.name.lower().endswith('.gz') else path.read_bytes()
    except (OSError, EOFError, gzip.BadGzipFile):
        return None
    text = data.decode('latin-1')
    return text if text.startswith('*PPD-Adobe') else None


def ppd_name(path: Path, source: str, root: Path) -> str:
    """The name CUPS lists the driver by; drivers elsewhere get a copy where CUPS looks."""
    rel = path.relative_to(root).as_posix()
    if under(path, root, LINKED_PPDS, CUPS_MODEL):
        return rel[len(CUPS_MODEL) + 1:]
    if under(path, root, LSB_PPDS):
        return 'lsb/usr/' + rel[len(LSB_PPDS) + 1:]
    target = root / LINKED_PPDS / slug(source) / path.name
    for n in range(2, 1000):
        if not target.exists():
            break
        target = target.with_name(f'{n}-{path.name}')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
    return target.relative_to(root / CUPS_MODEL).as_posix()


def ppd_programs(text: str) -> list[str]:
    """The filter programs a driver names (cupsFilter, cupsFilter2, cupsPreFilter)."""
    programs = []
    for value in re.findall(r'^\*cups(?:Filter2?|PreFilter):\s*"([^"]*)"', text, re.M):
        program = value.split()[-1] if value.split() else '-'
        if program != '-' and program not in programs:
            programs.append(program)
    return programs


def link_program(program: str, by_name: dict[str, list[Path]], root: Path, machine: str,
                 links: list[str]) -> list[str]:
    """Make a filter available where CUPS runs filters from, and check it can run."""
    if program.startswith('/'):
        path = root / program.lstrip('/')
        return check_program(path, root, machine) if path.exists() else [f'{program} is missing']
    path = root / FILTERS / program
    if path.exists():
        return check_program(path, root, machine)
    candidates = [p for p in by_name.get(program, []) if p.is_file()]
    if not candidates:
        return [f'Its filter "{program}" is not in what was added']
    best = max(candidates, key=lambda p: fit(p, machine))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    path.symlink_to(best)
    links.append(path.relative_to(root).as_posix())
    return check_program(path, root, machine)


def fit(path: Path, machine: str) -> int:
    head = elf_header(path)
    if head:
        return 3 if head[:2] == MACHINES.get(machine, (0, 0))[:2] else 0
    try:
        return 2 if path.read_bytes()[:2] == b'#!' else 1
    except OSError:
        return 0


def check_program(path: Path, root: Path, machine: str) -> list[str]:
    name = path.name
    real = path.resolve()
    try:
        head = real.read_bytes()[:256]
    except OSError:
        return [f'{name} is missing']
    if head.startswith(b'#!'):
        interpreter = head[2:].split(b'\n')[0].split()
        program = interpreter[0].decode(errors='replace') if interpreter else ''
        if program.endswith('/env') and len(interpreter) > 1:
            wanted = interpreter[1].decode(errors='replace')
            found = any((root / d / wanted).exists() for d in ('usr/bin', 'bin', 'usr/local/bin'))
            return [] if found else [f'{name} needs {wanted}, which is not installed']
        return [] if (root / program.lstrip('/')).exists() else [f'{name} needs {program}, which is not installed']
    info = elf_info(real)
    if info is None:
        return [] if os.access(real, os.X_OK) else [f'{name} is not a program']
    own = MACHINES.get(machine, (0, 0))
    if (info['machine'], info['bits']) != own[:2]:
        kind = ELF_MACHINES.get(info['machine'], f"machine {info['machine']}")
        if info['machine'] == 3 and own[0] == 62:
            missing = [lib for lib in info['needed'] if not find_library(lib, info, real.parent, root)]
            return [f'{name} is a 32-bit x86 program and needs 32-bit libraries: {", ".join(missing)}'] \
                if missing else []
        return [f'{name} is made for {kind} computers, not this one ({machine})']
    missing = [lib for lib in info['needed'] if not find_library(lib, info, real.parent, root)]
    return [f'{name} needs libraries that are not installed: {", ".join(missing)}'] if missing else []


# --- ELF files: which computer they are for, and what they need ----------------------

def elf_header(path: Path) -> tuple[int, int] | None:
    """(machine, bits) of an ELF file, or None."""
    try:
        with open(path, 'rb') as file:
            head = file.read(20)
    except OSError:
        return None
    if len(head) < 20 or head[:4] != b'\x7fELF':
        return None
    return struct.unpack_from('<H' if head[5] == 1 else '>H', head, 18)[0], 64 if head[4] == 2 else 32


def elf_info(path: Path) -> dict | None:
    """Machine, word size, needed libraries and run path of an ELF program."""
    header = elf_header(path)
    if header is None:
        return None
    data = path.read_bytes()
    big = data[5] == 2
    end = '>' if big else '<'
    is64 = header[1] == 64
    if is64:
        phoff, = struct.unpack_from(end + 'Q', data, 32)
        phentsize, phnum = struct.unpack_from(end + 'HH', data, 54)
    else:
        phoff, = struct.unpack_from(end + 'I', data, 28)
        phentsize, phnum = struct.unpack_from(end + 'HH', data, 42)
    loads, dynamic = [], None
    for i in range(phnum):
        offset = phoff + i * phentsize
        if is64:
            kind, _, p_offset, p_vaddr, _, p_filesz = struct.unpack_from(end + 'IIQQQQ', data, offset)
        else:
            kind, p_offset, p_vaddr, _, p_filesz = struct.unpack_from(end + 'IIIII', data, offset)
        if kind == 1:
            loads.append((p_vaddr, p_offset, p_filesz))
        elif kind == 2:
            dynamic = (p_offset, p_filesz)
    info = {'machine': header[0], 'bits': header[1], 'needed': [], 'runpath': []}
    if dynamic is None:
        return info
    entries, size, fmt = [], 16 if is64 else 8, end + ('qQ' if is64 else 'iI')
    for offset in range(dynamic[0], min(dynamic[0] + dynamic[1], len(data) - size + 1), size):
        tag, value = struct.unpack_from(fmt, data, offset)
        if tag == 0:
            break
        entries.append((tag, value))
    strtab = next((v for t, v in entries if t == 5), None)
    base = next((o + strtab - va for va, o, sz in loads if strtab is not None and va <= strtab < va + sz), None)
    if base is None:
        return info

    def string(at: int) -> str:
        start = base + at
        return data[start:data.index(b'\0', start)].decode(errors='replace')
    info['needed'] = [string(v) for t, v in entries if t == 1]
    info['runpath'] = [p for t, v in entries if t in (15, 29) for p in string(v).split(':') if p]
    return info


def standard_dirs(machine: str) -> list[str]:
    return ['lib', 'usr/lib', *LIBRARY_DIRS.get(MACHINES.get(machine, (0,))[0], [])]


def library_dirs(root: Path) -> list[str]:
    """The folders the system's loader searches (ld.so.conf and its includes)."""
    found: list[str] = []

    def read(conf: Path, depth: int = 0) -> None:
        try:
            lines = conf.read_text(errors='replace').splitlines()
        except OSError:
            return
        for line in lines:
            line = line.split('#')[0].strip()
            if line.startswith('include ') and depth < 3:
                pattern = line.split(None, 1)[1].lstrip('/')
                for included in sorted(root.glob(pattern)):
                    read(included, depth + 1)
            elif line.startswith('/'):
                found.append(line.lstrip('/'))
    read(root / 'etc' / 'ld.so.conf')
    return found


def find_library(name: str, info: dict, origin: Path, root: Path) -> bool:
    folders = [p.replace('$ORIGIN', str(origin)).replace('${ORIGIN}', str(origin)) for p in info['runpath']]
    candidates = [Path(f) if f.startswith(str(root)) and root != Path('/') else root / f.lstrip('/')
                  for f in folders]
    machine = next((m for m, (number, bits, _) in MACHINES.items()
                    if (number, bits) == (info['machine'], info['bits'])), '')
    candidates += [root / d for d in (*library_dirs(root), *standard_dirs(machine))]
    return any(elf_header(folder / name) == (info['machine'], info['bits']) for folder in candidates)


# --- The report -------------------------------------------------------------------------

def write_status(report: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(report), encoding='utf-8')
    tmp.chmod(0o644)  # Folio reads it as an ordinary user
    os.replace(tmp, STATUS_FILE)


def status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def added() -> frozenset[str]:
    """The drivers (ppd-names) that came from the drivers folder, for the built-in print server."""
    return frozenset(d['ppd'] for d in status().get('drivers', []) if isinstance(d, dict) and d.get('ppd'))


def summary(report: dict) -> list[str]:
    if report['state'] == 'none':
        return []
    if report['state'] in ('refused', 'failed'):
        return [report['message']]
    lines = [f"{s['name']}: {s['result']}" + (f" ({s['detail']})" if s['detail'] else '') for s in report['sources']]
    lines.append(f"{len(report['drivers'])} driver(s) added")
    return lines + [f'problem: {p}' for p in report['problems']]


if __name__ == '__main__':
    try:
        result = install()
    except Exception as exc:  # noqa: BLE001 - the print server starts either way
        result = {'state': 'failed', 'message': f'Installing from the drivers folder failed: {exc}',
                  'sources': [], 'drivers': [], 'problems': [], 'checked_at': time.time()}
    write_status(result)
    for line in summary(result):
        print(f'folio: drivers folder: {line}', flush=True)
    sys.exit(0)
