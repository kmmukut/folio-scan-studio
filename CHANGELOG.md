# Changelog

All notable changes to Folio. Versions follow `MAJOR.MINOR.PATCH`; until 1.0,
minor versions may add features and change defaults (always with upgrade notes).
Sections for 0.9.5 and earlier are carried over from the release READMEs.

## 0.11.0 — Printer management and print jobs

Folio can now set printers up in CUPS, and everyone can see what happened
to what they printed.

### Printer management (Devices → Printers)
- **Connect a CUPS administrator login** once per CUPS server. Folio checks
  it immediately, stores it in `data/cups-admin.json` (readable only by the
  container user), and never sends it back to a browser. If CUPS only
  accepts passwords over TLS, Folio switches to TLS automatically and trusts
  that server's certificate from then on; a changed certificate is refused
  before the password is sent.
- **Add a printer**: choose one CUPS detected (USB, or a network printer on
  the same network) or enter its address (IPP/AirPrint, JetDirect port 9100,
  or LPD). Pick **Driverless** for printers that support AirPrint/IPP
  Everywhere, or **Choose a driver**: select the manufacturer, type the
  model ("M2885FW"), and the matching drivers are listed first.
- **Test page, Pause/Resume, Jobs and Remove** on each printer card.
- Folio refuses to create a printer whose name already exists on that
  server (CUPS would silently replace it), and only uses devices CUPS
  reported or addresses it builds itself, never a typed device URI.
- Queues Folio creates are shared in CUPS. That is required: CUPS refuses
  jobs from other computers for unshared queues, and Folio is one.

### Print jobs
- Every print is now submitted under the Folio user's name (guests as
  `folio-guest`) instead of the container's `root`. CUPS's job history and
  page logs now show who printed what.
- **Print Desk → Your print jobs** shows your recent jobs with live status
  (Waiting, Printing, Stopped with the printer's reason, Done, Cancelled) and
  lets you cancel your own jobs. Folio checks ownership itself; if a CUPS
  server only lets administrators cancel, Folio uses the connected admin login.
- Administrators can open **Jobs** on a printer to see and cancel everyone's
  jobs (titles and owners need the admin login; CUPS hides them otherwise).
- System check lists whether each CUPS server has an admin login connected.

### Under the hood
- `backend/ipp.py`: a small IPP client (no new dependencies), needed because
  the CUPS command-line tools cannot take an admin password non-interactively.
- `backend/cupsadmin.py`: stored logins, discovery, drivers, queues and jobs.

### Verified
- 127 automated tests, against an in-process fake CUPS that speaks real IPP:
  login checks, TLS with certificate pinning, ownership of jobs, name
  clashes, input validation and an unreachable server.
- In headless Chrome against a real CUPS (a throwaway container built from
  the same image as a typical setup): connect, find printers, add a JetDirect
  printer with a Samsung M288x driver, add a driverless printer for a real
  Samsung M2885FW (CUPS read its capabilities: monochrome, duplex, 17 paper
  sizes, 3 trays; nothing was printed), test page, job list, cancel, pause,
  resume, remove. The test jobs went to an address that leads nowhere.
- **Not verified:** USB printers (no hardware available), and TLS against a
  real CUPS server (tested only against the fake server).

### Upgrading 0.10.0 → 0.11.0
No configuration changes. Rebuild or retag the image as
`folio-scan-studio:0.11.0` and redeploy. Existing queues keep working
unchanged. To manage printers from Folio, open **Devices** and connect your
CUPS administrator login (the `CUPSADMIN` user of an `anujdatar/cups`
container, or any user in CUPS's `lpadmin` group).

If your CUPS server restricts printing to particular user names, allow the
Folio user names (or `folio-guest`), since jobs no longer arrive as `root`.

## 0.10.0 — Background jobs: exports, OCR and printing that never time out

Exporting a PDF, making a searchable PDF, and printing from the web app now
run as background jobs. The page shows progress while the job runs, and a
reverse proxy can no longer cut it off. Nginx Proxy Manager, for example,
closes requests after 60 seconds by default, which a searchable export or a
large grayscale print on a small computer could easily exceed.

### What you will notice
- **Export** shows its progress on the button ("Exporting 40%") and the PDF
  downloads by itself when ready. Click the button again to cancel.
- **Searchable PDF** exports wait their turn instead of failing with
  "another searchable export is in progress". Cancelling stops text
  recognition immediately, including its Ghostscript and Tesseract helpers.
- **Print Desk** (uploads, saved documents and guest printing) shows each
  step: waiting, preparing or converting pages, sending to the printer, and
  then the CUPS receipt as before.
- Exports and prints run in separate queues, so a long export never delays a
  print. Each queue runs one job at a time to keep small hosts responsive.

### Under the hood
- Background exports write the PDF to disk as they go instead of holding the
  whole file in memory.
- The image now starts Folio under `tini`, a minimal init process that
  forwards stop signals and cleans up exited helper processes. Without it,
  a cancelled OCR job left zombie Ghostscript processes in the container.
- New API: `POST /api/jobs/{id}/exports`, `GET|DELETE /api/tasks/{id}`,
  `GET /api/tasks/{id}/file`, `GET /api/guest/tasks/{id}`, and
  `?background=true` on the print endpoints. The existing synchronous
  endpoints are unchanged, so scripts that call them keep working.
- Jobs and their files belong to the account (or guest session) that
  started them. Finished jobs are kept for 30 minutes.

### Verified
- 103 automated tests, including job ownership, queueing, cancellation,
  guest isolation, and a check that cancelling OCR stops every helper process.
- In headless Chrome: export (the PDF was saved by the browser), upload
  print, saved-document print, guest print at phone width, and System check.
  CUPS was simulated, so no printer was used.
- In the built image: a real searchable export (text layer present) and
  cancellation during text recognition, leaving no stray processes.

### Upgrading 0.9.6 → 0.10.0
No configuration changes. Back up `data/`, rebuild or retag the image as
`folio-scan-studio:0.10.0`, and redeploy. A job that is running when the
container restarts is lost: the page says so, and you can start it again.
`data/tasks/` holds temporary job files and is emptied at every start.

## 0.9.6 — Foundations: speed on small hosts, proxy-aware limits, diagnostics

Scanning, editing and printing work as before; this release makes them cheaper
to run and easier to troubleshoot, and adds a System check to Devices.

### Faster previews
- Page previews and thumbnails are rendered from a reduced preview copy of
  each scan and cached on disk, keyed by the page's edits. Previously every
  thumbnail decoded the full-resolution original, and every edit re-rendered
  every thumbnail in the page strip.
  Measured on a 20-page, 300 DPI colour document (18 MB per page, x86 host):
  opening it 5.6 s → 4.4 s; each edit afterwards 5.7 s → 0.2 s.
- At most `FOLIO_RENDER_CONCURRENCY` full-resolution decodes run at once
  (default 2, or 1 on single-core hosts), so loading many thumbnails can no
  longer spike memory on a small machine.
- Exported and printed PDFs still use the original scan pixels. The cache
  lives in `data/jobs/<document>/cache/`, is deleted with the document, and
  can be removed at any time; it is rebuilt on demand.

### Device queries
- CUPS queue lists, printer capabilities, SANE device lists and scanner
  capabilities are cached briefly instead of being re-queried on every page
  load and print. **Refresh devices** and **Refresh trays** still query
  immediately, as does **Test** for a scanner.
- An unreachable CUPS server is skipped for 60 s after a timeout. Measured
  with one offline server configured: listing printers took 16 s on every
  request in 0.9.5; now 8 s once, then about 0.01 s.
- Multiple CUPS servers are queried in parallel.

### Reverse proxies
- New `FORWARDED_ALLOW_IPS` setting (IPs or networks, e.g. `172.19.0.0/16`).
  Behind a reverse proxy, 0.9.5 saw every visitor as the proxy's address, so
  eight failed sign-ins by anyone locked out everyone for 15 minutes, and the
  per-visitor guest print limit applied to all guests together.
- `COOKIE_SECURE` can now be set from `.env`/Portainer variables.

### Diagnostics
- **Devices → System check** reports storage and memory, whether a reverse
  proxy is trusted, cookie settings, every CUPS server (reachability, queues),
  the default printer, SANE, each registered scanner and OCR availability,
  with a suggested fix for each problem. Read-only.
- `/api/health` now checks that the data directory is writable and the
  account database opens, and the image declares a Docker `HEALTHCHECK`
  (Portainer shows healthy/unhealthy). It no longer reveals the default
  scanner ID.

### Hardware independence
- **Breaking default:** there is no built-in CUPS server. 0.9.5 silently
  defaulted to one development address. Set `CUPS_SERVERS` explicitly; if it
  is empty, Print Desk and System check say so.
- The scanner status no longer says "Connected via WSD" for every scanner;
  it shows the scanner's actual protocol.

### Maintenance
- Python dependencies are pinned to exact, tested versions
  (`requirements.txt`); test-only tools moved to `requirements-dev.txt`
  (`pypdf` is no longer installed in the image).
- Replaced deprecated FastAPI startup/shutdown hooks and the deprecated
  `fitz` import name for PyMuPDF.
- Tests no longer write to `/data` on the machine running them, and a test
  guards against development network addresses appearing in shipped files.

### Upgrading 0.9.5 → 0.9.6
1. Back up the `data/` directory while no scan or print is running.
2. **Make sure `CUPS_SERVERS` is set** in your `.env` or Portainer stack
   variables (0.9.5 had a hidden default; 0.9.6 does not).
3. If Folio is behind a reverse proxy, set `FORWARDED_ALLOW_IPS` to the
   proxy's address or its Docker network. Do not use `*` if port 8870 is
   reachable directly, or clients could forge their address.
4. Rebuild and restart: `docker compose up -d --build`.
5. Open **Devices → Run checks** and resolve anything marked *Problem*.

Existing accounts, documents, scanner registrations and printer permissions
are unchanged. No data migration is needed; preview caches are created as
pages are viewed.

## 0.9.5 — Envelope compatibility and paper dimensions

All paper-size menus now display dimensions (both inches and millimeters). In
particular, **#10 is 4⅛ × 9½ in (105 × 241 mm)**, **DL is 110 × 220 mm**, and
**C5 is 162 × 229 mm**. Monarch and C6 are available if the chosen CUPS queue
advertises them. The selected size is also repeated below the dropdown, so
measurements remain visible even in a narrow three-column print form.

Older PPD drivers commonly advertise `Env10` rather than `COM10` and `EnvDL`
rather than `DL`. Folio now translates those **only when the CUPS queue actually
reports them**. For example, choosing #10 on a queue that advertises `Env10`
submits `-o PageSize=Env10`, with `InputSlot=MultiTray` when selected, rather than
blindly sending `media=COM10`. If the queue advertises a media type `Envelope`,
Folio includes it. The original input-tray option name and value are preserved.

An envelope is always printed **one-sided**. The interface guides users to choose
the multipurpose/manual feeder where the driver reports one. Printer-specific
paper-size/tray constraints are still enforced by CUPS and the printer: Folio
cannot verify an unadvertised envelope size from a model name alone. If #10
remains disabled, inspect the actual queue's PPD choices using:

```bash
docker exec cups lpoptions -p SamsungAirPrint -l | grep -Ei 'PageSize|InputSlot|MediaType'
```

Replace `SamsungAirPrint` with your actual CUPS queue, e.g. `HomePrinter`.
If you use a different CUPS server, run this on that server or use the `-h`
option. `lpoptions` should list the supported sizes; if `Env10` is missing,
check CUPS driver options rather than manually forcing a non-advertised size.

### Upgrading v0.9.4 → v0.9.5

When no scanning or printing is active, back up the persistent data directory,
extract the new zip over the existing `scan-studio` source, and rebuild:

```bash
cd /path/to/scan-studio
docker compose stop
tar -czf ../folio-before-v0.9.5.tgz data
cd ..
unzip -o folio-scan-studio-v0.9.5.zip
cd scan-studio
docker compose up -d --build
```

The archive does not contain `.env` or `data/`; existing accounts, scan files,
scanner registration, CUPS servers, and queue permissions remain intact.
Manual CUPS/PPD printer support is simulated in automated tests; a live #10
envelope print through your physical M2885FW has not been verified here.
## 0.9.4 — Uploaded-file tray selector visibility

The **signed-in and guest drag-and-drop workflows** now show a dedicated
**Paper source / input tray** selector directly under the file dropzone, before
the preview. You no longer need to scroll below a long PDF preview to find it.
The existing saved-document print dialog keeps the Paper source selector in its
print settings. All three paths still use the same validated `X-Print-Source`
request and CUPS `InputSlot`/`media-source` print option.

The selector always explains what CUPS reported. If it only offers **Printer
default**, your chosen queue is not advertising alternate trays to Folio;
that does **not** prove that the physical printer has no other feeders. Check
the correct CUPS queue's driver/options and press **Refresh trays** after
changes. This release also recognizes source options in legacy `lpoptions -l`
output that omit the human-readable `/Label` suffix, plus extra common
`InputTray`, `MediaSlot` and `PaperTray` spellings. Folio deliberately does
not fabricate a Tray 2 option if CUPS does not report it.

**Diagnostics on docker-host:** replace `CUPS_SERVER` and `QUEUE` with the
server and queue shown in Folio Devices; run from the Folio container:

```bash
docker exec scan-studio lpoptions -h CUPS_SERVER -p QUEUE -l | grep -Ei 'InputSlot|media-source|MediaSource|MediaPosition|PaperSource|InputTray|MediaSlot|PaperTray'
```

If no input-source option is listed, revisit the queue's driver and printer
configuration in CUPS. A software selector cannot switch to a tray the queue
does not know how to request. All prior data, CUPS servers and guest
permissions are retained across the update.


## 0.9.3 — Printer paper sources / input trays

Every Print Desk flow (signed-in upload, guest upload, and saved-document printing)
now includes **Paper source**, separately from **Paper size**. Options are loaded
for the selected CUPS queue through `lpoptions -p QUEUE -l` and updated when
you choose a different printer. Folio recognizes the IPP `media-source` option
and common PPD driver options such as `InputSlot`, `MediaSource`, and
`MediaPosition`. CUPS is still responsible for installing/configuring trays.

**Printer default** always works, even when a queue does not expose tray
choices. When CUPS advertises, for example, `Auto`, `Tray1`, `Tray2`, or
`Manual`, the selector offers only those values. Folio validates submitted
tray selections against the actual selected queue before constructing the
CUPS `lp -o InputSlot=Tray2` (or `lp -o media-source=...`) command. An unsupported
or stale tray selection is rejected rather than silently routed elsewhere.

The printer itself must have its optional trays installed and configured in
its CUPS driver. If you do not see a tray, check its CUPS options with:

```bash
lpoptions -h YOUR_CUPS_SERVER:631 -p YOUR_QUEUE -l | grep -Ei 'InputSlot|media-source|MediaSource|MediaPosition|PaperSource'
```

**Upgrade:** Stop Folio when idle, back up `scan-studio/data`, then extract
this v0.9.3 ZIP over your existing `scan-studio` source directory and run
`docker compose up -d --build`. Keep `.env` and `data/` unchanged. No new
environment variables or database migration are required. This feature
cannot guarantee that a physical tray is loaded with the right paper; that
remains a device/driver configuration detail.


## 0.9.2 — Scanner capabilities and multiple CUPS servers

- **Scanner capabilities fixed**: SANE's `scanimage --all-options` returns options on indented, multiline rows. Folio previously failed to parse these rows. Your Samsung should now show Flatbed, ADF, Color, Gray and 75/100/150/200/300 DPI. Press **Devices → Test** to verify. If it still says *Unavailable*, inspect `docker exec scan-studio scanimage -L` and `docker exec scan-studio scanimage -d 'YOUR_ACTUAL_DEVICE_NAME' --all-options`.
- **Multiple CUPS servers**: use `CUPS_SERVERS=198.51.100.2:631,203.0.113.70:631` (no spaces needed). Each server needs to be reachable from the Folio container on its CUPS TCP port, normally 631. Queues are uniquely identified by server and name; `OfficePrinter` on one CUPS server is not confused with `OfficePrinter` on another.
- **Printer defaults**: remove `CUPS_QUEUE` from your active `.env` and stack; select a queue under **Devices → Make default**. Existing v0.9.1 queue-only defaults and guest permissions migrate automatically if the queue name is unambiguous. When names collide across servers, select the desired queue explicitly in Devices.
- **Login**: the username field is empty on a fresh login page. The initial administrator name is still configured by `SCAN_STUDIO_ADMIN_USER` or defaults to `admin` for first-time setup, but it is not prefilled.
- **No printer administration privilege**: Folio reads queues and submits print jobs, but installs/driver configuration still happens in CUPS.

**After upgrading**, open Devices and use Refresh. If you had already entered a scanner, it remains in the `data/devices.json` registry. This release uses the same `data/` directory; back it up before replacing files.


## 0.9.1 — Clean installs and Folio-managed scanners

- **Clean first launch** contains no phantom scanner; the administrator adds the first scanner in Folio.
- No `SCANNER_DEVICE` value, `/etc/sane.d/airscan.conf` bind mount, or scanner-specific Compose configuration is required.
- **Devices → Add a network scanner** stores the WSD/eSCL IP, protocol, port, and endpoint under `./data/devices.json`. The first enabled scanner becomes the default automatically.
- **Edit / Test / Disable / Enable / Remove** work even for the only scanner, and changes take effect on the next SANE command without rebuilding/restarting the app. Scanner IDs remain stable when IPs change.
- Folio creates `./data/managed-airscan.conf` and SANE reads it through a symlink within the image. Multicast discovery is disabled by default to avoid slow probes across the many Docker bridges on this VLAN-heavy host; manual IP-based scanning continues to work. If you need to attach discovered eSCL devices on the same LAN, change `FOLIO_AIRSCAN_DISCOVERY` to `on` and restart the container.
- Multi-server CUPS printer discovery, print settings, guest printing, OCR, authentication, editing, PDF export, and the graceful scan cancellation path remain in place.

