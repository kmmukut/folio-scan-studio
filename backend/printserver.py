"""Folio's built-in print server: CUPS running next to Folio in its container.

With it, a new installation can print without setting up CUPS anywhere
else: Folio connects to it automatically as ``localhost:631``. Its printers,
drivers and settings live in ``data/cups``, so they survive upgrades. Other
CUPS servers can still be added in Devices, and FOLIO_BUILTIN_CUPS=off turns
the built-in one off entirely.

``python -m backend.printserver`` prepares the configuration and keeps
``cupsd`` running; the container starts it as root, before Folio. Folio's
web process only reads the generated administrator password.

The supervisor also keeps its printers announced on the network (AirPrint,
see airprint.py), and the scanners Folio shares (AirScan, see airscan.py),
checking every 30 seconds and withdrawing them when it stops.

CUPS's own web administration (https://<host>:631/admin) is off unless an
administrator turns it on in Folio with a login of their choosing. Folio,
which runs as an ordinary user, hands the request to this supervisor as a
file in data/print-server-requests; the supervisor creates the account,
deletes the request at once, and keeps only the password hash (root-only,
in data/cups) so the login survives restarts. Administration from other
computers then requires HTTPS; Folio's own connection stays local.

Folio must not be able to swap data/cups for a folder of its own, or it could
hand this root supervisor (and cupsd) a configuration it wrote. The container
start therefore gives the data folder itself to root, with Folio's group
allowed to add files and the sticky bit set: Folio keeps full use of its own
files but cannot rename or replace the print server's.
"""
from __future__ import annotations

import grp
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from backend import airprint, airscan, ipp

SERVER = 'localhost:631'
ADMIN_USER = 'folio-cups'
DEFAULTS = Path('/etc/cups')          # The cups package's own files, copied once
RESTART_DELAYS = (2, 5, 10, 30, 60)   # Seconds before restarting cupsd after it stops
ANNOUNCE_EVERY = 30                    # Seconds between AirPrint checks
WEB_ADMIN_NAME = re.compile(r'^[a-z][a-z0-9_-]{2,31}$')
RESERVED_NAMES = {'root', 'lp', 'folio', ADMIN_USER, 'daemon', 'bin', 'sys', 'nobody', 'saned', 'admin-folio'}
STATUS_FILE = Path(os.environ.get('FOLIO_RUN_DIR', '/run/folio')) / 'print-server.json'


def enabled() -> bool:
    return os.environ.get('FOLIO_BUILTIN_CUPS', 'on').strip().lower() not in ('off', 'false', 'no', '0')


def config_dir(data_dir: Path) -> Path:
    return Path(data_dir) / 'cups'


def secret_path(data_dir: Path) -> Path:
    return config_dir(data_dir) / 'folio-admin.secret'


def requests_dir(data_dir: Path) -> Path:
    """Where Folio leaves requests for the supervisor (Folio's own folder)."""
    return Path(data_dir) / 'print-server-requests'


def web_admin_path(data_dir: Path) -> Path:
    return config_dir(data_dir) / 'web-admin.json'


def web_admin(data_dir: Path) -> dict | None:
    """The CUPS web administration login, if turned on: {'username', 'hash'}."""
    try:
        login = json.loads(web_admin_path(data_dir).read_text(encoding='utf-8'))
        username, password_hash = str(login['username']), str(login['hash'])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if (not WEB_ADMIN_NAME.fullmatch(username) or username in RESERVED_NAMES
            or not password_hash.startswith('$') or ':' in password_hash or '\n' in password_hash):
        return None  # Never act on an account or hash this module would not have written
    return {'username': username, 'hash': password_hash}


def status() -> dict:
    """What the supervisor last reported (read by Folio)."""
    try:
        return json.loads(STATUS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def write_status(data_dir: Path, error: str | None = None) -> None:
    login = web_admin(data_dir)
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps({'web_admin': {'enabled': bool(login), 'username': login['username'] if login else None,
                                             'error': error, 'changed_at': time.time()}}), encoding='utf-8')
    tmp.chmod(0o644)  # Folio reads it as an ordinary user
    os.replace(tmp, STATUS_FILE)


def credentials(data_dir: Path) -> tuple[str, str] | None:
    """The administrator login Folio uses for the built-in server, once created."""
    try:
        password = secret_path(data_dir).read_text(encoding='utf-8').strip()
    except OSError:
        return None
    return (ADMIN_USER, password) if password else None


def cupsd_conf(*, port: int = 631, remote_admin: bool = False) -> str:
    """Folio's cupsd.conf: printing open to the network; administration local
    only, unless CUPS web administration is turned on (then over HTTPS)."""
    # Who may administer: Folio inside the container, plus, with web
    # administration on, anyone with an administrator login over HTTPS
    # (CUPS never asks local connections for encryption).
    admin_from = ('  Allow all\n  Encryption Required\n' if remote_admin else '  Allow 127.0.0.1\n  Allow ::1\n')
    limit_from = ('    Allow all\n    Encryption Required\n' if remote_admin else '    Allow 127.0.0.1\n    Allow ::1\n')
    return f"""# Written by Folio at every start; edits here are overwritten.
# Printing is open to anyone who can reach port {port} (only if it is published);
# administration is accepted from Folio itself{', and over HTTPS with a login' if remote_admin else ' only'}.
LogLevel warn
MaxLogSize 1m
PageLogFormat
Port {port}
Listen /run/cups/cups.sock
ServerAlias *
Browsing No
BrowseLocalProtocols none
DefaultShared Yes
DefaultAuthType Basic
WebInterface Yes
MaxJobs 500
PreserveJobHistory Yes
PreserveJobFiles No

<Location />
  Order allow,deny
  Allow all
</Location>
<Location /admin>
  AuthType Default
  Require user @SYSTEM
  Order allow,deny
{admin_from}</Location>
<Location /admin/conf>
  AuthType Default
  Require user @SYSTEM
  Order allow,deny
{admin_from}</Location>
<Location /admin/log>
  AuthType Default
  Require user @SYSTEM
  Order allow,deny
{admin_from}</Location>

<Policy default>
  JobPrivateAccess default
  JobPrivateValues default
  SubscriptionPrivateAccess default
  SubscriptionPrivateValues default
  <Limit Create-Job Print-Job Print-URI Validate-Job>
    Order deny,allow
  </Limit>
  <Limit Send-Document Send-URI Hold-Job Release-Job Restart-Job Purge-Jobs Set-Job-Attributes Create-Job-Subscription Renew-Subscription Cancel-Subscription Get-Notifications Reprocess-Job Cancel-Current-Job Suspend-Current-Job Resume-Job Cancel-My-Jobs Close-Job CUPS-Move-Job>
    Require user @OWNER @SYSTEM
    Order deny,allow
  </Limit>
  <Limit CUPS-Get-Document>
    AuthType Default
    Require user @OWNER @SYSTEM
    Order deny,allow
  </Limit>
  <Limit CUPS-Add-Modify-Printer CUPS-Delete-Printer CUPS-Add-Modify-Class CUPS-Delete-Class CUPS-Set-Default CUPS-Get-Devices>
    AuthType Default
    Require user @SYSTEM
    Order allow,deny
{limit_from}  </Limit>
  <Limit Pause-Printer Resume-Printer Enable-Printer Disable-Printer Pause-Printer-After-Current-Job Hold-New-Jobs Release-Held-New-Jobs Deactivate-Printer Activate-Printer Restart-Printer Shutdown-Printer Startup-Printer Promote-Job Schedule-Job-After Cancel-Jobs CUPS-Accept-Jobs CUPS-Reject-Jobs>
    AuthType Default
    Require user @SYSTEM
    Order allow,deny
{limit_from}  </Limit>
  <Limit Cancel-Job CUPS-Authenticate-Job>
    Require user @OWNER @SYSTEM
    Order deny,allow
  </Limit>
  <Limit All>
    Order deny,allow
  </Limit>
</Policy>
"""


def cups_files_conf(server_root: Path) -> str:
    return f"""# Written by Folio at every start; edits here are overwritten.
ServerRoot {server_root}
RequestRoot {server_root / 'spool'}
CacheDir /var/cache/cups
StateDir /run/cups
TempDir /var/spool/cups/tmp
SystemGroup root lpadmin
User lp
Group lp
LogFileGroup adm
AccessLog /var/log/cups/access_log
ErrorLog /var/log/cups/error_log
PageLog /var/log/cups/page_log
FileDevice No
"""


def _run(*command: str, stdin: str | None = None) -> None:
    subprocess.run(command, input=stdin, text=True, check=True, capture_output=True, timeout=30)


def ensure_admin_account(password: str) -> None:
    """A system account in CUPS's lpadmin group, recreated at every start because
    the container's user list is not kept between restarts."""
    try:
        _run('id', ADMIN_USER)
    except subprocess.CalledProcessError:
        _run('useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin',
             '--groups', 'lpadmin', ADMIN_USER)
    _run('chpasswd', stdin=f'{ADMIN_USER}:{password}\n')


def _account_exists(username: str) -> bool:
    return subprocess.run(['id', '-u', username], capture_output=True).returncode == 0


def _shadow_hash(username: str) -> str:
    for line in Path('/etc/shadow').read_text(encoding='utf-8').splitlines():
        name, _, rest = line.partition(':')
        if name == username:
            return rest.split(':', 1)[0]
    raise OSError(f'No password stored for {username}')


def apply_web_admin(data_dir: Path, request: dict) -> str | None:
    """Turn CUPS web administration on (with a login) or off; returns a
    problem to report, or None."""
    current = web_admin(data_dir)
    if not request.get('enabled'):
        if current:
            subprocess.run(['userdel', current['username']], capture_output=True)
            web_admin_path(data_dir).unlink(missing_ok=True)
        return None
    username, password = str(request.get('username', '')), str(request.get('password', ''))
    if not WEB_ADMIN_NAME.fullmatch(username) or username in RESERVED_NAMES:
        return 'Choose another username: 3–32 lowercase letters, digits, - or _, starting with a letter'
    if not 12 <= len(password) <= 256 or ':' in password or '\n' in password:
        return 'The password needs 12–256 characters (no colons or line breaks)'
    if username != (current or {}).get('username') and _account_exists(username):
        return 'That name belongs to a system account; choose another'
    if not _account_exists(username):
        _run('useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin', '--groups', 'lpadmin', username)
    _run('chpasswd', stdin=f'{username}:{password}\n')
    if current and current['username'] != username:
        subprocess.run(['userdel', current['username']], capture_output=True)
    path = web_admin_path(data_dir)
    path.write_text(json.dumps({'username': username, 'hash': _shadow_hash(username)}), encoding='utf-8')
    path.chmod(0o600)
    return None


def process_requests(data_dir: Path, port: int = 631) -> bool:
    """Apply a request Folio left (CUPS web administration); True if CUPS must
    reload its configuration. The request, which holds a password, is deleted
    before anything else."""
    path = requests_dir(data_dir) / 'web-admin.json'
    if not os.path.lexists(path):
        return False
    try:  # Folio's folder: never follow a link it may have put there
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), encoding='utf-8') as handle:
            request = json.loads(handle.read(4096))
    except (OSError, ValueError):
        request = None
    finally:
        path.unlink(missing_ok=True)
    try:
        error = apply_web_admin(data_dir, request) if isinstance(request, dict) else 'Unreadable request'
    except (OSError, subprocess.SubprocessError) as exc:
        error = f'Could not change the CUPS login: {exc}'
    root = config_dir(data_dir)
    (root / 'cupsd.conf').write_text(cupsd_conf(port=port, remote_admin=bool(web_admin(data_dir))), encoding='utf-8')
    write_status(data_dir, error)
    return True


def prepare(data_dir: Path, *, port: int = 631, system: bool = True) -> Path:
    """Write the configuration and the administrator login; returns the config folder.

    ``system`` creates the administrator account and sets ownership, which
    needs root; tests that only check the files pass False.
    """
    root = config_dir(data_dir)
    if system:
        claim(root)
    for folder in (root, root / 'ppd', root / 'ssl', root / 'spool', root / 'spool' / 'tmp'):
        folder.mkdir(parents=True, exist_ok=True)
    # First start: bring over the package's defaults (MIME rules, SNMP settings…).
    if DEFAULTS.is_dir():
        for item in DEFAULTS.iterdir():
            target = root / item.name
            if item.name in ('cupsd.conf', 'cups-files.conf', 'printers.conf', 'classes.conf') or target.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
    login = web_admin(data_dir)
    (root / 'cupsd.conf').write_text(cupsd_conf(port=port, remote_admin=bool(login)), encoding='utf-8')
    (root / 'cups-files.conf').write_text(cups_files_conf(root), encoding='utf-8')
    secret = secret_path(data_dir)
    if not credentials(data_dir):
        secret.write_text(secrets.token_urlsafe(24), encoding='utf-8')
    secret.chmod(0o600)
    if system:
        # Folio runs as an ordinary user in the FOLIO_GID group and must read it.
        shutil.chown(secret, 'root', int(os.environ.get('FOLIO_GID', '0')))
        secret.chmod(0o640)
        for folder in ('/run/cups', '/var/cache/cups', '/var/log/cups', '/var/spool/cups/tmp'):
            Path(folder).mkdir(parents=True, exist_ok=True)
        shutil.chown('/var/spool/cups/tmp', 'root', 'lp')
        Path('/var/spool/cups/tmp').chmod(0o1770)
        allow_passing_through(Path(data_dir))
        # cupsd refuses files it does not consider its own.
        for path, mode in ((root, 0o755), (root / 'ppd', 0o755), (root / 'ssl', 0o700),
                           (root / 'cupsd.conf', 0o640), (root / 'cups-files.conf', 0o640),
                           (root / 'spool', 0o710), (root / 'spool' / 'tmp', 0o1770)):
            shutil.chown(path, 'root', 'lp')
            path.chmod(mode)
        ensure_admin_account(credentials(data_dir)[1])
        if login:  # The container's accounts don't survive a restart; the hash does
            if not _account_exists(login['username']):
                _run('useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin', '--groups', 'lpadmin',
                     login['username'])
            _run('usermod', '-p', login['hash'], login['username'])
        write_status(data_dir)
    return root


def claim(root: Path) -> None:
    """data/cups and everything in it belong to root. If something there does
    not (the print server was switched off and someone else created it), take
    it back without following links, and remove links, which cupsd would follow."""
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        root.unlink()
    if not root.exists():
        return
    lp = grp.getgrnam('lp').gr_gid  # CUPS's own group, as for everything it writes there
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in [folder, *(os.path.join(folder, n) for n in dirs + files)]:
            info = os.lstat(name)
            if stat.S_ISLNK(info.st_mode):
                os.unlink(name)
            elif info.st_uid != 0:
                os.lchown(name, 0, lp)
                os.chmod(name, info.st_mode & 0o7755)  # No longer writable by its old owner's group or others


def allow_passing_through(data_dir: Path) -> None:
    """CUPS's helpers run as the unprivileged lp user and must reach data/cups.
    Others may pass through the data folder but not list or read it (its group
    is Folio's own); Folio keeps its own files private (see app.keep_private)."""
    mode = data_dir.stat().st_mode
    data_dir.chmod((mode & 0o7770) | 0o011)


def cupsd_command(root: Path) -> list[str]:
    return ['cupsd', '-f', '-c', str(root / 'cupsd.conf'), '-s', str(root / 'cups-files.conf')]


def announce(data_dir: Path, server: str = SERVER) -> list[str]:
    """Announce the shared printers (AirPrint) and scanners (AirScan), and record
    the result for Folio."""
    if not airprint.available():
        airprint.write_status(announced=[])
        airscan.write_status(announced=[])
        return []
    try:
        airscan.write_status(announced=airscan.sync_announcements(data_dir))
    except OSError as exc:
        airscan.write_status(announced=[], error=str(exc)[:200])
    try:
        login = credentials(data_dir)
        announced = airprint.sync(ipp.Connection(server, *(login or (None, None)), timeout=10))
    except (ipp.IPPError, OSError) as exc:
        airprint.write_status(announced=[], error=str(exc)[:200])
        return []
    airprint.write_status(announced=announced)
    return announced


def supervise(data_dir: Path) -> int:
    """Run cupsd until asked to stop, restarting it with a growing delay if it exits,
    and keep its printers announced while it runs."""
    stopping = False
    child: subprocess.Popen | None = None

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if child and child.poll() is None:
            child.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    failures = 0
    while not stopping:
        try:
            root = prepare(data_dir)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f'folio: built-in print server could not be prepared: {exc}', file=sys.stderr, flush=True)
            root = None
        if root is not None:
            started = time.monotonic()
            child = subprocess.Popen(cupsd_command(root))
            next_announcement = started + 3  # Give cupsd a moment to load its printers
            while child.poll() is None and not stopping:
                if process_requests(data_dir):
                    child.send_signal(signal.SIGHUP)  # cupsd re-reads its configuration
                if time.monotonic() >= next_announcement:
                    announce(data_dir)
                    next_announcement = time.monotonic() + ANNOUNCE_EVERY
                time.sleep(.5)
            child.wait()
            if time.monotonic() - started > 300:
                failures = 0  # It ran fine for a while; start over with short delays
        if stopping:
            break
        delay = RESTART_DELAYS[min(failures, len(RESTART_DELAYS) - 1)]
        failures += 1
        print(f'folio: built-in print server stopped; restarting in {delay} s', file=sys.stderr, flush=True)
        deadline = time.monotonic() + delay
        while not stopping and time.monotonic() < deadline:
            time.sleep(.2)
    airprint.withdraw()  # Don't leave phones looking for printers (or scanners) that are gone
    airprint.write_status(announced=[])
    airscan.write_status(announced=[])
    return 0


if __name__ == '__main__':
    if not enabled():
        sys.exit(0)
    sys.exit(supervise(Path(os.environ.get('DATA_DIR', '/data'))))
