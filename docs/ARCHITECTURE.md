# Folio architecture

Folio's purpose: **replace obsolete software, not functioning hardware.** A
small Linux computer should be enough to give an older USB or network
printer/scanner a modern, private, network-wide experience.

## The three layers

| Layer | Responsibility | Implemented by |
|---|---|---|
| **Device** | Talk to hardware over USB, IPP, JetDirect, LPD, eSCL, WSD… | CUPS, SANE (incl. `sane-airscan`), Avahi — standard Linux services |
| **Sharing** | Let any phone or computer print and scan *without Folio's UI*: driverless printing (AirPrint / IPP Everywhere / Mopria) and network scanning (eSCL / saned) | The same services, configured by Folio |
| **Workflow** | Scan workbench, non-destructive editing, document library, Print Desk, accounts, diagnostics | Folio's web app (this repository) |

Most of what makes an old printer feel modern comes from the sharing layer.
Folio's job is to make the device and sharing layers easy to set up and keep
healthy, and to make the workflow layer excellent.

## Current structure (0.18)

- `backend/app.py`: FastAPI routes, auth middleware, scan and print flows.
- `backend/devices.py`: scanner registry (`data/devices.json`), generated
  `sane-airscan` config, read-only CUPS discovery and capability parsing,
  short-lived device-query cache with CUPS backoff.
- `backend/printing.py`: print-file validation, page selection, scaling,
  colour conversion, media/tray naming.
- `backend/processing.py`: non-destructive page transforms and deskew.
- `backend/security.py`: SQLite accounts/sessions, guest tokens, rate limits.
- `backend/diagnostics.py`: read-only System check.
- `backend/tasks.py`: background job runner (export and print lanes),
  progress, cancellation, ownership and cleanup.
- `backend/features.py`: which optional features this installation has
  (Lite/Full editions, D10).
- `backend/ipp.py`: minimal IPP client (CUPS administration and job queries).
- `backend/cupsadmin.py`: stored CUPS admin logins, discovery, drivers,
  queue management and per-user print jobs.
- `backend/stats.py`: Folio's print log and the statistics built from it
  (D12); `backend/cupsimport.py` optionally adds jobs sent straight to CUPS.
- `backend/printserver.py`: the built-in print server: writes CUPS's
  configuration into `data/cups`, creates Folio's own administrator login,
  and keeps `cupsd` running (D14).
- `backend/airprint.py`: announces the built-in printers through the host's
  Avahi services folder (D14).
- `backend/backup.py`: backups of everything Folio keeps, and restoring one,
  including the built-in printers with their drivers (D15).
- `backend/destinations.py`: sending finished documents to Paperless-ngx or
  a mounted folder (D16).
- `docker-entrypoint.sh`: starts the print-server supervisor as root, then
  Folio as the `PUID` user (D15).
- `frontend/`: framework-free HTML/CSS/JS, served as static files.

Storage: page originals are immutable PNGs under `data/jobs/<id>/pages/`;
edits are metadata in `job.json`; previews are a disposable cache under
`data/jobs/<id>/cache/`. Accounts live in `data/access.sqlite3`; the print
log in `data/print-log.sqlite3`.

## Decisions

### D1 — Docker is the only supported deployment (2026-10)
One supported path keeps the setup and upgrade story simple and testable.
No native/systemd installer. USB access, when needed, is granted to a device
container, never to the web app container.

### D2 — Folio may create and manage CUPS queues (2026-10; built in 0.11)
Reverses the 0.9.x rule that Folio never administers CUPS. Setting up a
printer without knowing CUPS requires creating queues. Folio talks IPP to
any configured CUPS server using an administrator login entered in Devices
(stored 0600 in `data/`, never returned to browsers, TLS with certificate
pinning when CUPS requires encryption). Driverless (IPP Everywhere) first,
legacy drivers as a fallback. The web container still gets no device
access; USB stays with whichever container runs CUPS.

### D9 — Jobs carry the Folio user's name (0.11)
Folio submits with `lp -U <user>` (guests: `folio-guest`). CUPS then shows
each person their own jobs and lets them cancel them, and its page log
records who printed. Folio verifies ownership itself before acting.

### D3 — Git is the source of truth (2026-10)
Releases are tagged commits; release archives are built from tags. Runtime
data (`data/`) and secrets (`.env`) are never committed.

### D4 — Keep the stack small
No frontend build step or framework, no external database or message queue.
SQLite plus in-process background work is sufficient for the expected scale
(a household or small office). Heavy features (OCR, deskew/OpenCV) are
optional at build time; see D10.

### D5 — Previews are disposable, originals are sacred
Previews render from a cached reduced copy; exports and printing always use
the original pixels. Any cache can be deleted without data loss.

### D7 — Slow work runs as background jobs (0.10)
Anything that can outlast a reverse proxy's timeout (export, OCR, print
preparation) runs in an in-process job runner with two lanes, export and
print, each running one job at a time. Jobs are in memory by design: a
restart loses in-flight work, the UI says so, and no job state has to be
migrated. Synchronous endpoints stay available for scripts.

### D8 — Split `app.py` gradually, not in one release (0.10)
Splitting the 1,900-line `app.py` into route modules was planned for 0.10
and postponed. Most tests patch attributes of `backend.app` directly, so a
one-step split means rewriting the tests that would check the split, and
users would see nothing from it. New features go into their own modules
(`tasks.py`, `diagnostics.py`), and existing areas move out when they are
next changed substantially.

### D10 — Lite and Full editions from one codebase (0.12)
A build argument (`FOLIO_EDITION`) decides which optional packages go into
the image; the code is identical. At runtime `features.py` detects what is
installed and the API and page adapt, so nothing branches on the edition
name. Optional Python modules are imported lazily, and a test imports the
app with them blocked. The base stays `python:3.12-slim` (Debian): Alpine
was measured and rejected because PyMuPDF has no musl ARM64 wheel and
OpenCV no musl wheels, while the OS layer is not where the size is.

### D11 — USB support grows with the community (2026-10)
No USB printers or scanners are available to the maintainers for testing,
so USB-specific work (passthrough recipes, IPP-over-USB, per-model quirks)
is left to community contributors with hardware rather than built
untested. Folio's design already allows it: CUPS and SANE own the devices.

### D12 — Print statistics come from Folio's own log, in SQLite (0.13)
Folio records each job it hands to CUPS in a separate SQLite file. CUPS's
page log was considered and rejected: it is off by default (as on the
maintainer's server), it lives inside the CUPS container where Folio cannot
read it, and it would mix in jobs from other computers. Folio's own log is
exact for what Folio sent, works with any CUPS server, and stays isolated:
it can be switched off or deleted without touching accounts. No document
names are stored.

PostgreSQL was considered and rejected for the same reasons as in D4. It
would add a second container (about 30–60 MB of memory at rest), a password
to manage, its own backups (`pg_dump`) and dump-and-restore on every major
version upgrade, all for a table that grows by a few thousand rows a year.
Its strengths (many writers, several app instances, network access for
reporting tools) don't apply: Folio is one process. SQLite in WAL mode
handles this volume with room to spare, is backed up by copying `data/`,
and the CSV export covers spreadsheets and reporting tools. The SQL is kept
plain, so the log could move to another database later if a real need
appears.

### D13 — Published images, built and tested by CI (0.14)
Folio is published under the AGPL-3.0-or-later (PyMuPDF and Ghostscript,
which the images ship, are AGPL-3.0). Images go to GHCR, mirrored to Docker
Hub, for amd64 and arm64 only: there is no 32-bit ARM wheel for PyMuPDF,
so a 32-bit build would compile MuPDF every time. Each architecture is built
on a native GitHub runner rather than under emulation, and the full test
suite runs inside every image on its own architecture before anything is
published. Tags carry the edition (`0.14.0-full`, `0.14-lite`); `latest`
is the newest Full release. The base image is pinned by digest and updated
through Dependabot pull requests, so every rebuild is deliberate and tested.

### D14 — A print server inside Folio (0.15)
Folio's image now includes CUPS and every printer driver Debian packages,
in both editions (Lite: +229 MB, Full: +81 MB; old printers on small hosts
need legacy drivers more than they need OCR). A small supervisor, started
as root before Folio, writes CUPS's configuration into `data/cups`, creates
a system account with a generated password for Folio, and restarts `cupsd`
if it stops. Folio treats it as the print server `localhost:631`: always
listed, always connected, never removable (FOLIO_BUILTIN_CUPS=off turns it
off). CUPS on other computers keeps working exactly as before.

This reverses D2's "the Folio container gets no device access": USB scanners
already needed the device in Folio's container, so one container owning USB
printers and scanners is simpler and as safe. To limit what a compromised
document converter (CUPS runs them as the unprivileged `lp` user) could
reach, Folio's own files are private (0600/0700, umask 077) and the data
folder is pass-through only for other users.

Printers are announced to phones (AirPrint) by writing Avahi service files
into the host's `/etc/avahi/services`, mounted into the container. Mounting
the host's D-Bus would let CUPS announce by itself, but it gives a root
container root-equivalent control of the host through D-Bus; a folder of
announcements gives nothing else away.

### D15 — The first minutes: browser setup, no root, backups (0.16)
A new installation no longer needs a password in its settings: the first
visitor creates the administrator in the browser. That is possible only in
the first 15 minutes after Folio starts (a restart reopens it), so a
forgotten installation reachable from elsewhere can't be claimed later; the
check and the insert are one database transaction.

Folio's web application runs as an ordinary user (`PUID`/`PGID`, default
1000), which also makes the data folder belong to that user on the host. The
start-up script, still root, hands Folio its files and starts the print
server's supervisor, which stays root: cupsd needs it, the host's Avahi
folder belongs to root, and it owns the AirPrint announcements (refreshed
every 30 seconds, withdrawn on stop, reported to Folio through a status
file). Folio reads its login to the print server through group permission.
`PUID=0` is the escape hatch for devices only root may open.

Backups are one archive Folio writes and reads itself: consistent SQLite
copies (without sign-ins), settings, logins and documents (not caches), and
the built-in printers with their PPD files, fetched and recreated over IPP
(CUPS-Get-PPD, CUPS-Add-Modify-Printer with the file attached). Folio never
needs to read CUPS's root-owned files to do it. A restore keeps what it
replaces in data/.before-restore, swaps item by item, and keeps the person
restoring signed in if their account is in the backup.

### D16 — Documents go where you keep them (0.17)
Folio stays a capture-and-print desk, not an archive: finished documents
are sent onward. Paperless-ngx is reached through its upload API with a
token (Python's standard library only, no new dependency); a folder is any
host folder mounted under /destinations, written atomically so folder
watchers never see a partial file. Sending reuses the export job: the PDF
is built in the background, then delivered.

Blank pages are found without OpenCV, so Lite has it too: at 1200 px wide,
ink is whatever is clearly darker than the paper's median shade, and a page
is blank only if no 40 px patch holds 3 % ink or more. Measured on test
pages, dust (up to 2 %) and show-through (0 %) stay below that, while a
signature (8 %), initials (5 %) or one small line (9 %) never do. Blank
pages are moved to the deleted pages, never discarded, so Undo restores
them; duplex batches keep theirs, because fronts and backs must stay paired.

### D6 — Capabilities come from the device, never from model names
Sizes, trays, duplex, colour, scan modes and resolutions are what CUPS/SANE
report. Folio does not fabricate options, and no device, address or network
is hardcoded (enforced by a test).

## Direction

Planned in this order; each step is a separate, tested release. Priorities
may change with real use.

1. ~~**0.10 — Background jobs.**~~ Done; see D7 and D8.
2. ~~**0.11 — Printer management.**~~ Done; see D2 and D9.
3. ~~**0.12 — Lite and Full editions.**~~ Done; see D10.
4. ~~**0.13 — Print statistics.**~~ Done; see D12.
5. ~~**0.14 — Public release.**~~ Published images, CI, community docs; see D13.
6. ~~**0.15 — Folio is the print server.**~~ Built-in CUPS with drivers,
   AirPrint announcements, printer supplies; see D14.
7. ~~**0.16 — The first minutes.**~~ Administrator created in the browser,
   setup checklist, no root, backup and restore; see D15.
8. ~~**0.17 — Scans go where you keep documents.**~~ Paperless-ngx and folder
   destinations, blank pages; see D16.
9. ~~**0.18 — Print quotas.**~~ Pages per person per calendar month, counted
   from the print log (so they need statistics kept); checked after the
   page range is applied and before anything reaches CUPS. Administrators
   have none; guests keep their own limits.
10. **Next candidates:** a Raspberry Pi check and community hardware reports
   toward 1.0; translations; saved scan presets.
USB-specific work waits for community testers (D11).

Before 1.0: a fresh Raspberry Pi plus a supported USB printer reaches a first
print in under 10 minutes; upgrades preserve data; a compatibility list
exists. (Running Folio as a non-root user was done in 0.16.)
