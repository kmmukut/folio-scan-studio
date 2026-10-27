"""Folio's built-in print server: CUPS running next to Folio in its container.

With it, a new installation can print without setting up CUPS anywhere
else: Folio connects to it automatically as ``localhost:631``. Its printers,
drivers and settings live in ``data/cups``, so they survive upgrades. Other
CUPS servers can still be added in Devices, and FOLIO_BUILTIN_CUPS=off turns
the built-in one off entirely.

``python -m backend.printserver`` prepares the configuration and keeps
``cupsd`` running; the container starts it as root, before Folio. Folio's
web process only reads the generated administrator password.

Announcing its printers on the network (AirPrint) is airprint.py's job.
"""
from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

SERVER = 'localhost:631'
ADMIN_USER = 'folio-cups'
DEFAULTS = Path('/etc/cups')          # The cups package's own files, copied once
RESTART_DELAYS = (2, 5, 10, 30, 60)   # Seconds before restarting cupsd after it stops


def enabled() -> bool:
    return os.environ.get('FOLIO_BUILTIN_CUPS', 'on').strip().lower() not in ('off', 'false', 'no', '0')


def config_dir(data_dir: Path) -> Path:
    return Path(data_dir) / 'cups'


def secret_path(data_dir: Path) -> Path:
    return config_dir(data_dir) / 'folio-admin.secret'


def credentials(data_dir: Path) -> tuple[str, str] | None:
    """The administrator login Folio uses for the built-in server, once created."""
    try:
        password = secret_path(data_dir).read_text(encoding='utf-8').strip()
    except OSError:
        return None
    return (ADMIN_USER, password) if password else None


def cupsd_conf(*, port: int = 631) -> str:
    """Folio's cupsd.conf: printing open to the network, administration local only."""
    return f"""# Written by Folio at every start; edits here are overwritten.
# Printing is open to anyone who can reach port {port} (only if it is published);
# administration is accepted only from Folio itself, with its own login.
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
  Allow 127.0.0.1
  Allow ::1
</Location>
<Location /admin/conf>
  AuthType Default
  Require user @SYSTEM
  Order allow,deny
  Allow 127.0.0.1
  Allow ::1
</Location>
<Location /admin/log>
  AuthType Default
  Require user @SYSTEM
  Order allow,deny
  Allow 127.0.0.1
  Allow ::1
</Location>

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
    Allow 127.0.0.1
    Allow ::1
  </Limit>
  <Limit Pause-Printer Resume-Printer Enable-Printer Disable-Printer Pause-Printer-After-Current-Job Hold-New-Jobs Release-Held-New-Jobs Deactivate-Printer Activate-Printer Restart-Printer Shutdown-Printer Startup-Printer Promote-Job Schedule-Job-After Cancel-Jobs CUPS-Accept-Jobs CUPS-Reject-Jobs>
    AuthType Default
    Require user @SYSTEM
    Order allow,deny
    Allow 127.0.0.1
    Allow ::1
  </Limit>
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


def prepare(data_dir: Path, *, port: int = 631, system: bool = True) -> Path:
    """Write the configuration and the administrator login; returns the config folder.

    ``system`` creates the administrator account and sets ownership, which
    needs root; tests that only check the files pass False.
    """
    root = config_dir(data_dir)
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
    (root / 'cupsd.conf').write_text(cupsd_conf(port=port), encoding='utf-8')
    (root / 'cups-files.conf').write_text(cups_files_conf(root), encoding='utf-8')
    secret = secret_path(data_dir)
    if not credentials(data_dir):
        secret.write_text(secrets.token_urlsafe(24), encoding='utf-8')
    secret.chmod(0o600)
    if system:
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
    return root


def allow_passing_through(data_dir: Path) -> None:
    """CUPS's helpers run as the unprivileged lp user and must reach data/cups.
    Others may pass through the data folder but not list or read it; Folio keeps
    its own files private (see app.ensure_directories)."""
    mode = data_dir.stat().st_mode
    data_dir.chmod((mode & 0o7700) | 0o011)


def cupsd_command(root: Path) -> list[str]:
    return ['cupsd', '-f', '-c', str(root / 'cupsd.conf'), '-s', str(root / 'cups-files.conf')]


def supervise(data_dir: Path) -> int:
    """Run cupsd until asked to stop, restarting it with a growing delay if it exits."""
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
    return 0


if __name__ == '__main__':
    if not enabled():
        sys.exit(0)
    sys.exit(supervise(Path(os.environ.get('DATA_DIR', '/data'))))
