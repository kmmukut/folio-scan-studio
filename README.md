# Folio Scan Studio v0.9.5 — Multi-device printing and envelope compatibility

The Samsung M2885FW is **not** hardcoded anywhere in the new Docker deployment. ScanservJS is not required. Folio uses `scanimage` + `sane-airscan` directly and maintains an editable scanner registry through **Devices**. CUPS continues to manage printer queues and drivers independently.

## What changed from v0.9

- **Clean first launch** contains no phantom scanner; the administrator adds the first scanner in Folio.
- No `SCANNER_DEVICE` value, `/etc/sane.d/airscan.conf` bind mount, or scanner-specific Compose configuration is required.
- **Devices → Add a network scanner** stores the WSD/eSCL IP, protocol, port, and endpoint under `./data/devices.json`. The first enabled scanner becomes the default automatically.
- **Edit / Test / Disable / Enable / Remove** work even for the only scanner, and changes take effect on the next SANE command without rebuilding/restarting the app. Scanner IDs remain stable when IPs change.
- Folio creates `./data/managed-airscan.conf` and SANE reads it through a symlink within the image. Multicast discovery is disabled by default to avoid slow probes across the many Docker bridges on this VLAN-heavy host; manual IP-based scanning continues to work. If you need to attach discovered eSCL devices on the same LAN, change `FOLIO_AIRSCAN_DISCOVERY` to `on` and restart the container.
- Multi-server CUPS printer discovery, print settings, guest printing, OCR, authentication, editing, PDF export, and the graceful scan cancellation path remain in place.

## Clean installation on your existing Docker VM

1. **If you have existing scans or users to keep, back up the old `scan-studio/data` directory before removing anything.** This ZIP intentionally has no data files or credentials. If you reuse your old `data/` directory, Folio retains accounts and documents; the old v0.9 `original` scanner will show as offline until you select **Edit** and enter its IP to convert it to a Folio-managed scanner. To start totally fresh, use a new empty `data/` directory instead.
2. Extract the ZIP into `/path/to/`, creating the `scan-studio` folder. If v0.9 is already deployed under the same name, stop the old stack first; do not run two scan containers concurrently.
3. Create credentials and start:

   ```bash
   cd /path/to/scan-studio
   cp .env.example .env  # Only if .env does not already exist
   nano .env              # Set SCAN_STUDIO_ADMIN_PASSWORD
   chmod 600 .env
   docker compose up -d --build
   ```

4. Open `http://<DOCKER-VM-IP>:8870` (or your existing Proxmox port forwarding / SSH tunnel) and log in. Go to **Devices → Add a network scanner**. For your working Samsung M2885FW:

   | Setting | Value |
   |---|---|
   | Name | Samsung M2885FW |
   | IP address | 192.0.2.124 |
   | Protocol | WSD |
   | Port | 8018 |
   | Endpoint path | /wsd/scan |

   Press **Add scanner**. The first scanner becomes the default. Press **Test**; verify modes `Color`, `Gray`, sources `Flatbed`, `ADF`, and the supported DPIs. Then open Workbench and scan one flatbed page before using ADF.
5. The CUPS server(s) are listed in `CUPS_SERVERS` as a comma-separated list, e.g. `198.51.100.2:631,203.0.113.70:631`. Folio lists installed queues; it does not create CUPS queues. Choose your default printer in **Devices**. You do not need to know a printer queue name in advance. Two printers with the same queue name on different servers are displayed distinctly.

### Portainer note

The included Compose file uses `build: .` because Folio is a local source project, not a published registry image. Run `docker compose up -d --build` from the extracted source directory. You can then manage the running container with Portainer. If you want to deploy from Portainer's web stack editor, build the `folio-scan-studio:0.9.5` image on the Docker host first, then remove `build: .` from the stack editor and use that local `image:` tag.

## Editing a scanner later

In **Devices**, choose **Edit** on your scanner, update the IP/port/path, then Save and Test. The generated scanner configuration is persisted at:

- `./data/devices.json` — source of truth for registered scanners
- `./data/managed-airscan.conf` — automatically generated SANE configuration; don't edit manually

You can inspect device detection inside the running container:

```bash
docker exec scan-studio scanimage -L
docker exec scan-studio scanimage --all-options -d 'airscan:w0:Folio-...'  # Use actual device name from the previous command
```

If it reports offline after a successful port test, review `docker logs scan-studio --tail 80` and try the exact WSD/eSCL endpoint returned by your scanner's discovery diagnostics. A working TCP connection alone does not prove SANE can negotiate a scan session.

### Permissions and security

Device administration requires an administrator session; ordinary members cannot edit scanner endpoints. CUPS printer permissions and guest printing are still configured through Folio. Anonymous web printing remains disabled by default and should be exposed only to trusted LAN/VPN users. Keep Folio behind an HTTPS reverse proxy or VPN when exposing it outside a trusted network, and set `COOKIE_SECURE=true` under HTTPS.

### Automated testing

```bash
python -m pip install -r requirements.txt pytest httpx
python -m pytest tests -q
node --check frontend/app.js
```

The tests simulate scanner discovery, capability queries, scanned pages and CUPS submission. **Live physical WSD scanning and a complete Samsung print job still need testing on your host.**


## v0.9.2 fixes and migration

- **Scanner capabilities fixed**: SANE's `scanimage --all-options` returns options on indented, multiline rows. Folio previously failed to parse these rows. Your Samsung should now show Flatbed, ADF, Color, Gray and 75/100/150/200/300 DPI. Press **Devices → Test** to verify. If it still says *Unavailable*, inspect `docker exec scan-studio scanimage -L` and `docker exec scan-studio scanimage -d 'YOUR_ACTUAL_DEVICE_NAME' --all-options`.
- **Multiple CUPS servers**: use `CUPS_SERVERS=198.51.100.2:631,203.0.113.70:631` (no spaces needed). Each server needs to be reachable from the Folio container on its CUPS TCP port, normally 631. Queues are uniquely identified by server and name; `OfficePrinter` on one CUPS server is not confused with `OfficePrinter` on another.
- **Printer defaults**: remove `CUPS_QUEUE` from your active `.env` and stack; select a queue under **Devices → Make default**. Existing v0.9.1 queue-only defaults and guest permissions migrate automatically if the queue name is unambiguous. When names collide across servers, select the desired queue explicitly in Devices.
- **Login**: the username field is empty on a fresh login page. The initial administrator name is still configured by `SCAN_STUDIO_ADMIN_USER` or defaults to `admin` for first-time setup, but it is not prefilled.
- **No printer administration privilege**: Folio reads queues and submits print jobs, but installs/driver configuration still happens in CUPS.

**After upgrading**, open Devices and use Refresh. If you had already entered a scanner, it remains in the `data/devices.json` registry. This release uses the same `data/` directory; back it up before replacing files.


## v0.9.3 — Printer paper sources / input trays

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


## v0.9.4 — Uploaded-file tray selector visibility

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


## v0.9.5 — Envelope compatibility and paper dimensions

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
