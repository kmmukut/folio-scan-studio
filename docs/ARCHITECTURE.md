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

## Current structure (0.12)

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
- `frontend/`: framework-free HTML/CSS/JS, served as static files.

Storage: page originals are immutable PNGs under `data/jobs/<id>/pages/`;
edits are metadata in `job.json`; previews are a disposable cache under
`data/jobs/<id>/cache/`. Accounts live in `data/access.sqlite3`.

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
4. **Next candidates:** usage statistics from CUPS's page log (jobs now
   carry user names); arm64 image builds; an optional bundled CUPS + Avahi
   container with AirPrint advertising and a first-run wizard. USB-specific
   work waits for community testers (D11).

Before 1.0: a fresh Raspberry Pi plus a supported USB printer reaches a first
print in under 10 minutes; upgrades preserve data; a compatibility list
exists; the container runs as a non-root user.
