"""v0.23: printer makers' own drivers, from a folder mounted read-only at
/drivers and installed at every start. Most tests install into a folder that
stands in for the system; the last one installs a real Debian package and
prints through it with a real CUPS, inside the image."""
from __future__ import annotations

import gzip
import io
import os
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import airprint, app as web, cupsadmin, devices, diagnostics, drivers, printserver, security, tasks  # noqa: E402

MACHINE = os.uname().machine
OTHER = 'aarch64' if MACHINE == 'x86_64' else 'x86_64'
FILTER = "#!/bin/sh\n# A stand-in filter: marks its output, then passes the page on.\nprintf 'ACME-DRIVER\\n'\nif [ -n \"$6\" ]; then cat \"$6\"; else cat; fi\n"
needs_dpkg = pytest.mark.skipif(not shutil.which('dpkg-deb'), reason='needs dpkg-deb')
needs_bsdtar = pytest.mark.skipif(not shutil.which('bsdtar'), reason='needs bsdtar (in the image)')


def ppd(nickname: str, program: str | None = None, maker: str = 'Acme') -> str:
    lines = ['*PPD-Adobe: "4.3"', '*FormatVersion: "4.3"', '*FileVersion: "1.0"', '*LanguageVersion: English',
             '*LanguageEncoding: ISOLatin1', '*PCFileName: "ACME.PPD"', f'*Manufacturer: "{maker}"',
             '*Product: "(Acme)"', f'*ModelName: "{nickname}"', '*ShortNickName: "Acme"',
             f'*NickName: "{nickname}"', '*PSVersion: "(3010.000) 0"', '*LanguageLevel: "3"',
             '*ColorDevice: False', '*DefaultColorSpace: Gray', '*FileSystem: False', '*Throughput: "1"',
             '*LandscapeOrientation: Plus90', '*TTRasterizer: Type42',
             '*OpenUI *PageSize: PickOne', '*DefaultPageSize: Letter', '*PageSize Letter: ""', '*CloseUI: *PageSize',
             '*DefaultImageableArea: Letter', '*ImageableArea Letter: "0 0 612 792"',
             '*DefaultPaperDimension: Letter', '*PaperDimension Letter: "612 792"']
    if program:
        lines.insert(5, f'*cupsFilter: "application/vnd.cups-pdf 0 {program}"')
    return '\n'.join(lines) + '\n'


@pytest.fixture
def system(tmp_path):
    """A folder standing in for the system, and the drivers folder."""
    root = tmp_path / 'system'
    (root / 'bin').mkdir(parents=True)
    (root / 'bin' / 'sh').write_text('')  # Scripts' interpreter
    folder = tmp_path / 'drivers'
    folder.mkdir()
    return root, folder


def install(folder, root, machine=MACHINE):
    return drivers.install(folder, root, machine=machine, read_only_required=False)


def tarball(path: Path, files: dict[str, tuple[bytes | str, int]]) -> Path:
    with tarfile.open(path, 'w:gz') as archive:
        for name, (data, mode) in files.items():
            data = data.encode() if isinstance(data, str) else data
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            archive.addfile(info, io.BytesIO(data))
    return path


def deb(path: Path, package: str, arch: str, files: dict[str, tuple[str, int]]) -> Path:
    build = path.parent / f'build-{package}-{arch}'
    (build / 'DEBIAN').mkdir(parents=True)
    (build / 'DEBIAN' / 'control').write_text(f'Package: {package}\nVersion: 1.0\nArchitecture: {arch}\n'
                                              'Maintainer: Acme <acme@example.com>\nDescription: A test driver\n')
    for name, (data, mode) in files.items():
        target = build / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data)
        target.chmod(mode)
    subprocess.run(['dpkg-deb', '--root-owner-group', '-b', str(build), str(path)], check=True, capture_output=True)
    shutil.rmtree(build)
    return path


def real_library(name: str) -> Path:
    for folder in (*drivers.library_dirs(Path('/')), *drivers.standard_dirs(MACHINE)):
        path = Path('/') / folder / name
        if drivers.elf_header(path) == drivers.MACHINES[MACHINE][:2]:
            return path
    pytest.skip(f'no {name} for this computer')


# --- What the folder holds --------------------------------------------------------

def test_nothing_happens_without_drivers(tmp_path, system):
    root, folder = system
    assert install(tmp_path / 'missing', root)['state'] == 'none'
    assert install(folder, root)['state'] == 'none'
    (folder / '.DS_Store').write_text('')
    assert install(folder, root)['state'] == 'none'


def test_a_folder_folio_could_change_is_not_used(system):
    root, folder = system
    (folder / 'Acme.ppd').write_text(ppd('Acme Laser'))
    report = drivers.install(folder, root, machine=MACHINE)  # A test folder is writable: not mounted :ro
    assert report['state'] == 'refused' and ':ro' in report['message']
    assert not (root / drivers.LINKED_PPDS).exists() and report['drivers'] == []


def test_a_ppd_is_a_driver_and_comes_first(system):
    root, folder = system
    (folder / 'Acme-Laser.ppd').write_text(ppd('Acme Laser 500 Series'))
    (folder / 'Acme-Old.ppd.gz').write_bytes(gzip.compress(ppd('Acme Laser 400 Series').encode()))
    report = install(folder, root)
    assert report['state'] == 'done' and report['problems'] == []
    assert {d['ppd']: d['name'] for d in report['drivers']} == {
        'folio-drivers/ppd/Acme-Laser.ppd': 'Acme Laser 500 Series',
        'folio-drivers/ppd/Acme-Old.ppd.gz': 'Acme Laser 400 Series'}
    assert (root / drivers.CUPS_MODEL / 'folio-drivers/ppd/Acme-Laser.ppd').read_text().startswith('*PPD-Adobe')
    candidates = [{'ppd': 'open', 'name': 'Acme Laser 500 Foomatic/pxlmono (recommended)', 'language': 'en'},
                  {'ppd': 'folio-drivers/ppd/Acme-Laser.ppd', 'name': 'Acme Laser 500 Series', 'language': 'en'}]
    added = {d['ppd'] for d in report['drivers']}
    assert cupsadmin.rank_drivers(candidates, 'Acme Laser 500', preferred=added)[0]['ppd'] == \
        'folio-drivers/ppd/Acme-Laser.ppd'
    assert cupsadmin.rank_drivers(candidates, 'Acme Laser 500')[0]['ppd'] == 'open'  # As before without it


def test_an_archive_with_its_filter_and_library(system):
    root, folder = system
    library = real_library('libc.so.6').read_bytes()
    tarball(folder / 'acme-driver.tar.gz', {
        'acme/ppd/Acme_X.ppd': (ppd('Acme X Series', 'acmefilter'), 0o644),
        'acme/bin/acmefilter': (FILTER, 0o755),
        'acme/lib/libacme.so.1': (library, 0o644)})
    report = install(folder, root)
    assert report['problems'] == [] and report['sources'][0]['result'] == 'unpacked'
    assert [d['ppd'] for d in report['drivers']] == ['folio-drivers/acme-driver.tar.gz/Acme_X.ppd']
    link = root / drivers.FILTERS / 'acmefilter'
    assert link.is_symlink() and link.resolve() == root / drivers.UNPACKED / 'acme-driver.tar.gz/acme/bin/acmefilter'
    assert (root / drivers.LD_CONF).read_text() == '/opt/folio-drivers/acme-driver.tar.gz/acme/lib\n'
    # Taken out of the folder: gone at the next start.
    (folder / 'acme-driver.tar.gz').unlink()
    assert install(folder, root)['state'] == 'none'
    assert not link.is_symlink() and not (root / drivers.LD_CONF).exists()
    assert not (root / drivers.UNPACKED).exists() and not (root / drivers.LINKED_PPDS).exists()


def test_programs_are_checked_for_this_computer(system):
    root, folder = system
    program = Path(shutil.which('true', path='/usr/bin:/bin'))
    tarball(folder / 'acme.tar.gz', {'acme/Acme_E.ppd': (ppd('Acme E Series', 'acmebin'), 0o644),
                                     'acme/acmebin': (program.read_bytes(), 0o755)})
    # The system has none of the libraries it needs (on ARM, the loader too)…
    needed = drivers.elf_info(program)['needed']
    assert 'libc.so.6' in needed
    assert install(folder, root)['problems'] == [f"acmebin needs libraries that are not installed: {', '.join(needed)}"]
    # …now it has.
    folder_for_libraries = root / drivers.standard_dirs(MACHINE)[-1]
    folder_for_libraries.mkdir(parents=True)
    for name in needed:
        shutil.copyfile(real_library(name), folder_for_libraries / name)
    assert install(folder, root)['problems'] == []
    # A program for another kind of computer is named as such.
    (problem,) = install(folder, root, machine=OTHER)['problems']
    assert problem.startswith('acmebin is made for ') and f'not this one ({OTHER})' in problem
    # Scripts need their interpreter.
    tarball(folder / 'acme.tar.gz', {'acme/Acme_E.ppd': (ppd('Acme E Series', 'acmebin'), 0o644),
                                     'acme/acmebin': ('#!/usr/bin/perl\nprint;\n', 0o755)})
    assert install(folder, root)['problems'] == ['acmebin needs /usr/bin/perl, which is not installed']


def test_a_driver_whose_filter_is_missing_says_so(system):
    root, folder = system
    (folder / 'Acme.ppd').write_text(ppd('Acme F Series', 'acme-missing-filter'))
    assert install(folder, root)['problems'] == ['Its filter "acme-missing-filter" is not in what was added']


@needs_dpkg
def test_debian_packages_for_this_computer(system):
    root, folder = system
    files = {'opt/acme/ppd/Acme_D.ppd': (ppd('Acme D Series', 'acmefilter'), 0o644),
             'opt/acme/bin/acmefilter': (FILTER, 0o755)}
    deb(folder / 'acme-driver_1.0_all.deb', 'acme-driver', 'all', files)
    other = drivers.MACHINES[OTHER][2]
    deb(folder / f'acme-other_1.0_{other}.deb', 'acme-other', other, files)
    report = install(folder, root)
    results = {s['name']: (s['result'], s['detail']) for s in report['sources']}
    assert results['acme-driver_1.0_all.deb'] == ('installed', '')
    assert results[f'acme-other_1.0_{other}.deb'][0] == 'skipped'
    assert f'Made for {other} computers' in results[f'acme-other_1.0_{other}.deb'][1]
    assert [d['ppd'] for d in report['drivers']] == ['folio-drivers/acme-driver_1.0_all.deb/Acme_D.ppd']
    assert (root / drivers.FILTERS / 'acmefilter').resolve() == root / 'opt/acme/bin/acmefilter'
    assert report['problems'] == []


@needs_dpkg
def test_packages_inside_an_archive_debian_first(system, tmp_path):
    root, folder = system
    native = drivers.MACHINES[MACHINE][2]
    package = deb(tmp_path / 'acme.deb', 'acme-driver', native,
                  {'usr/share/cups/model/acme/Acme_N.ppd': (ppd('Acme N Series'), 0o644)})
    tarball(folder / 'acme-linux.tar.gz', {f'{native}/Debian/acme-driver.deb': (package.read_bytes(), 0o644),
                                           'x86_64/RPM/acme-driver.x86_64.rpm': (b'not used', 0o644)})
    report = install(folder, root)
    assert [(s['name'], s['result']) for s in report['sources']] == [
        ('acme-linux.tar.gz', 'unpacked'), ('acme-linux.tar.gz: acme-driver.deb', 'installed')]
    assert [d['ppd'] for d in report['drivers']] == ['acme/Acme_N.ppd']  # Where CUPS already looks


def cpio(files: dict[str, tuple[bytes, int]]) -> bytes:
    out = b''
    for number, (name, (data, mode)) in enumerate([*files.items(), ('TRAILER!!!', (b'', 0))], 1):
        encoded = name.encode() + b'\0'
        fields = (number, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(encoded), 0)
        out += b'070701' + ''.join(f'{v:08x}' for v in fields).encode() + encoded
        out += b'\0' * (-len(out) % 4) + data
        out += b'\0' * (-len(out) % 4)
    return out


def rpm(files: dict[str, tuple[bytes, int]]) -> bytes:
    """The smallest RPM bsdtar reads: the lead, two empty headers, the files."""
    lead = struct.pack('>4sBBhh66shh16s', b'\xed\xab\xee\xdb', 3, 0, 0, 1, b'acme-driver', 1, 5, b'')
    header = b'\x8e\xad\xe8\x01' + bytes(4) + struct.pack('>II', 0, 0)
    return lead + header + header + gzip.compress(cpio(files))


@needs_bsdtar
def test_rpm_packages_are_unpacked_but_never_over_folio(system):
    root, folder = system
    (folder / 'acme-driver-1.0-1.noarch.rpm').write_bytes(rpm({
        './opt/acme/ppd/Acme_R.ppd': (ppd('Acme R Series', 'acmefilter').encode(), 0o100644),
        './opt/acme/bin/acmefilter': (FILTER.encode(), 0o100755),
        './app/backend/app.py': (b'replaced', 0o100644)}))
    report = install(folder, root)
    assert report['sources'][0]['result'] == 'unpacked', report['sources']
    assert [d['ppd'] for d in report['drivers']] == ['folio-drivers/acme-driver-1.0-1.noarch.rpm/Acme_R.ppd']
    assert os.access(root / 'opt/acme/bin/acmefilter', os.X_OK) and report['problems'] == []
    assert not (root / 'app').exists()
    assert drivers.rpm_fits('acme-1.0-1.x86_64.rpm', 'x86_64') and not drivers.rpm_fits('acme-1.0-1.x86_64.rpm', 'aarch64')
    assert drivers.rpm_fits('acme-1.0-1.noarch.rpm', 'aarch64')


@pytest.mark.parametrize('vendor', ['Samsung', 'HP'])
def test_the_unified_linux_driver_is_set_up_like_its_installer(system, vendor):
    root, folder = system
    files = {'uld/noarch/oem.conf': (f'VENDOR={vendor}\n', 0o644),
             'uld/noarch/share/ppd/Samsung_M288x_Series.ppd': (ppd('Samsung M288x Series', 'rastertospl'), 0o644),
             'uld/noarch/share/ppd/cms/M288xsc.cts': ('tuning', 0o644)}
    for arch in ('x86_64', 'i386'):
        files[f'uld/{arch}/rastertospl'] = (FILTER, 0o755)
        files[f'uld/{arch}/libscmssc.so'] = ('engine', 0o755)
    tarball(folder / 'uld_V1.00.39_01.17.tar.gz', files)
    report = install(folder, root, machine='x86_64')
    model = f'uld-{vendor.lower()}'
    assert report['sources'][0]['detail'] == "the maker's Unified Linux Driver"
    assert [d['ppd'] for d in report['drivers']] == [f'{model}/Samsung_M288x_Series.ppd']
    printer = root / 'opt/smfp-common/printer'
    assert (printer / 'lib/libscmssc.so').read_text() == 'engine'
    assert (root / drivers.CUPS_MODEL / model / 'cms/M288xsc.cts').read_text() == 'tuning'
    assert (root / drivers.FILTERS / 'rastertospl').resolve() == printer / 'bin/rastertospl'
    assert not (root / drivers.UNPACKED / 'uld_V1.00.39_01.17.tar.gz').exists()
    # Samsung made it for x86 only.
    report = install(folder, root, machine='aarch64')
    assert report['sources'][0]['result'] == 'failed'
    assert 'exists for x86 computers only, not this one (aarch64)' in report['sources'][0]['detail']


def test_a_root_folder_is_copied_but_never_over_folio(system):
    root, folder = system
    (folder / 'root/usr/share/cups/model/mine').mkdir(parents=True)
    (folder / 'root/usr/share/cups/model/mine/Mine.ppd').write_text(ppd('Mine 1 Series'))
    for protected in ('app/backend/app.py', 'data/users.db'):
        (folder / 'root' / protected).parent.mkdir(parents=True)
        (folder / 'root' / protected).write_text('replaced')
    report = install(folder, root)
    assert report['sources'][0]['result'] == 'partly' and "Folio's own): app, data" in report['sources'][0]['detail']
    assert [d['ppd'] for d in report['drivers']] == ['mine/Mine.ppd']
    assert not (root / 'app').exists() and not (root / 'data').exists()


def test_unsafe_archives_are_refused(system, tmp_path):
    root, folder = system
    tarball(folder / 'escape.tar.gz', {'../escaped.ppd': (ppd('Escape'), 0o644)})
    with zipfile.ZipFile(folder / 'escape.zip', 'w') as archive:
        archive.writestr('../../escaped.ppd', ppd('Escape'))
    report = install(folder, root)
    assert [s['result'] for s in report['sources']] == ['failed', 'failed']
    assert 'Unsafe path' in report['sources'][1]['detail']
    assert not list(tmp_path.rglob('escaped.ppd'))


def test_a_zip_keeps_its_programs_runnable(system):
    root, folder = system
    with zipfile.ZipFile(folder / 'acme.zip', 'w') as archive:
        archive.writestr('Acme_Z.ppd', ppd('Acme Z Series', 'acmezip'))
        program = zipfile.ZipInfo('bin/acmezip')
        program.external_attr = 0o100755 << 16
        archive.writestr(program, FILTER)
    report = install(folder, root)
    assert report['problems'] == [] and os.access(root / drivers.FILTERS / 'acmezip', os.X_OK)


def test_other_files_are_skipped_and_said_so(system):
    root, folder = system
    (folder / 'setup.exe').write_bytes(b'MZ')
    (folder / 'other').mkdir()
    report = install(folder, root)
    assert {s['name']: s['result'] for s in report['sources']} == {'other': 'skipped', 'setup.exe': 'skipped'}
    assert report['drivers'] == []


# --- What Folio shows -----------------------------------------------------------------

def test_system_check_and_the_driver_list_read_the_report(system, monkeypatch, tmp_path):
    root, folder = system
    monkeypatch.setattr(drivers, 'STATUS_FILE', tmp_path / 'run' / 'drivers.json')
    assert drivers.added() == frozenset() and diagnostics.check_drivers() == []
    (folder / 'Acme.ppd').write_text(ppd('Acme Laser 500 Series'))
    drivers.write_status(install(folder, root))
    assert drivers.added() == {'folio-drivers/ppd/Acme.ppd'}
    (result,) = diagnostics.check_drivers()
    assert result['status'] == 'ok' and result['detail'] == '1 driver added: Acme Laser 500 Series.'
    (folder / 'Acme-F.ppd').write_text(ppd('Acme F Series', 'acme-missing'))
    (folder / 'setup.exe').write_bytes(b'MZ')
    drivers.write_status(install(folder, root))
    (result,) = diagnostics.check_drivers()
    assert result['status'] == 'warn' and 'setup.exe: Not a kind of driver' in result['detail']
    assert 'Its filter "acme-missing" is not in what was added' in result['detail']
    drivers.write_status(drivers.install(folder, root, machine=MACHINE))  # Not read-only
    (result,) = diagnostics.check_drivers()
    assert result['status'] == 'warn' and ':ro' in result['detail']
    assert drivers.summary(drivers.status()) == [drivers.status()['message']]


def test_the_folder_is_mounted_read_only_and_installed_before_the_print_server():
    compose = (ROOT / 'compose.yaml').read_text()
    assert '- "${FOLIO_DRIVERS_DIR:-./drivers}:/drivers:ro"' in compose
    assert '\nFOLIO_DRIVERS_DIR=./drivers\n' in (ROOT / '.env.example').read_text()
    entrypoint = (ROOT / 'docker-entrypoint.sh').read_text()
    assert entrypoint.index('python -m backend.drivers || true') < entrypoint.index('python -m backend.printserver &')
    dockerfile = (ROOT / 'Dockerfile').read_text()
    assert 'libcupsimage2t64 libarchive-tools' in dockerfile  # What makers' drivers need, and RPM unpacking


# --- A real package through a real CUPS (inside the image, as root) ---------------------

def login(username, password):
    client = TestClient(web.app)
    response = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert response.status_code == 200, response.text
    client.headers['x-csrf-token'] = response.json()['csrf']
    return client


def follow(client, task, timeout=10):
    deadline = time.monotonic() + timeout
    while task['status'] not in tasks.FINISHED:
        assert time.monotonic() < deadline, task
        time.sleep(.05)
        task = client.get(f"/api/tasks/{task['id']}").json()
    return task


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@pytest.mark.skipif(not shutil.which('cupsd') or not shutil.which('dpkg') or not hasattr(os, 'geteuid')
                    or os.geteuid() != 0, reason='needs cupsd, dpkg and root: runs inside the Folio image')
def test_a_makers_package_prints_through_the_built_in_server(monkeypatch):
    tmp_path = Path(tempfile.mkdtemp(prefix='folio-drivers-', dir='/var/tmp'))
    folder = tmp_path / 'drivers'
    folder.mkdir()
    deb(folder / 'acme-test-driver_1.0_all.deb', 'acme-test-driver', 'all', {
        'opt/acme-test/ppd/Acme_Test.ppd': (ppd('Acme Test 100 Series', 'acmetestfilter'), 0o644),
        'opt/acme-test/bin/acmetestfilter': (FILTER, 0o755)})
    received, sink = bytearray(), socket.socket()
    sink.bind(('127.0.0.1', 0))
    sink.listen(4)

    def collect():  # A stand-in JetDirect printer: keeps whatever it is sent
        while True:
            try:
                conn, _ = sink.accept()
            except OSError:
                return
            with conn:
                while chunk := conn.recv(65536):
                    received.extend(chunk)
    threading.Thread(target=collect, daemon=True).start()
    cupsd = None
    try:
        report = drivers.install(folder, Path('/'), read_only_required=False)
        assert [s['result'] for s in report['sources']] == ['installed'], report
        assert report['problems'] == [] and (Path('/') / drivers.FILTERS / 'acmetestfilter').is_symlink()
        monkeypatch.setattr(drivers, 'STATUS_FILE', tmp_path / 'drivers.json')
        drivers.write_status(report)
        port = free_port()
        root = printserver.prepare(tmp_path, port=port)
        cupsd = subprocess.Popen(printserver.cupsd_command(root))
        server = f'127.0.0.1:{port}'
        for _ in range(100):  # (lpstat -r exits 0 either way; only its words tell)
            answer = subprocess.run(['lpstat', '-h', server, '-r'], capture_output=True, text=True).stdout
            if answer.strip() == 'scheduler is running':
                break
            time.sleep(.2)
        monkeypatch.setenv('FOLIO_BUILTIN_CUPS', 'on')
        monkeypatch.setattr(printserver, 'SERVER', server)
        monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
        monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
        monkeypatch.setattr(web, 'DEVICE', '')
        monkeypatch.setattr(web, 'CUPS_SERVERS', [])
        monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', 'drivers-admin-long-password')
        monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
        monkeypatch.setattr(airprint, 'SERVICES_DIR', tmp_path / 'no-avahi')
        security._guest_submissions.clear()
        with TestClient(web.app):
            admin = login('admin', 'drivers-admin-long-password')
            path = f'/api/admin/cups/servers/{quote(server, safe="")}'
            listed = admin.get(f'{path}/drivers', params={'make': 'Acme', 'model': 'Test 100'}).json()['drivers']
            assert listed[0]['ppd'] == 'folio-drivers/acme-test-driver_1.0_all.deb/Acme_Test.ppd', listed
            started = admin.post(f'{path}/printers', json={
                'name': 'Acme_Test', 'connection': 'socket', 'address': '127.0.0.1',
                'port': sink.getsockname()[1], 'driver': listed[0]['ppd'], 'make_default': True})
            assert started.status_code == 202, started.text
            assert follow(admin, started.json()['task'], timeout=60)['status'] == 'done'
            with fitz.open() as document:
                document.new_page().insert_text((72, 100), 'Through a maker’s own driver')
                page = document.tobytes()
            token = admin.post('/api/print/inspect', headers={'X-Upload-Filename': 'page.pdf'},
                               content=page).json()['token']
            receipt = admin.post('/api/print/submit-preview', json={'token': token})
            assert receipt.status_code == 200, receipt.text
            for _ in range(150):
                if b'%%EOF' in received:
                    break
                time.sleep(.2)
            log = Path('/var/log/cups/error_log')
            assert received.startswith(b'ACME-DRIVER\n%PDF'), log.read_text()[-3000:] if log.exists() else received[:200]
    finally:
        if cupsd:
            cupsd.terminate()
            cupsd.wait(timeout=20)
        sink.close()
        subprocess.run(['dpkg', '-r', 'acme-test-driver'], capture_output=True)
        empty = tmp_path / 'empty'
        empty.mkdir(exist_ok=True)
        drivers.install(empty, Path('/'), read_only_required=False)  # Takes its links away again
        tasks.runner.start(tmp_path / 'tasks')
        devices.invalidate()
        shutil.rmtree(tmp_path, ignore_errors=True)
