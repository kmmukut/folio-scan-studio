# Folio Scan Studio

**Modern features. Existing hardware. Minimal resources.**

Folio turns printers and scanners that Linux can drive (through CUPS and
SANE) into a private, browser-based print and scan workstation. Run it on a
Raspberry Pi–class computer, a mini PC or an existing Docker host, and use
it from any phone, tablet or computer on your network.

- **Scan:** flatbed and document feeder, colour or grayscale, multipage
  documents, manual duplex (fronts then backs), cancel and recover.
- **Edit without losing the original:** crop, rotate, straighten,
  brightness/contrast/auto-levels, reorder, delete and undo. The original
  scan is never modified.
- **Export:** PDF, optionally searchable (OCR), as a background job with
  progress, so long exports aren't cut off by a reverse proxy.
- **Set up printers:** add USB and network printers to CUPS from Folio,
  driverless or with a driver picked by model, then test, pause or remove them.
- **Print:** upload a PDF or image, or print a saved document, with
  page ranges, paper size and tray, envelopes, duplex, scaling, pages per
  sheet and colour mode — limited to what each printer actually reports.
- **Follow your jobs:** see whether each print is waiting, printing, stopped
  (and why) or done, and cancel it.
- **Share safely:** administrator, member and print-only accounts, private
  documents, and optional rate-limited guest printing.

Folio unlocks what the hardware can do; it cannot add features the device
lacks (a monochrome printer will not print in colour).

## Editions

One codebase, two images. Pick with `FOLIO_EDITION` in `.env`:

- **Full** (default, ~1.2 GB): everything, including searchable PDFs (OCR)
  and automatic straightening suggestions.
- **Lite** (~660 MB): for Raspberry Pi–class computers. Identical except it
  leaves out OCR and automatic straightening; manual straightening, PDF
  export, printing and printer management are all there.

## Requirements

- Docker with Compose (Portainer works too).
- A computer running CUPS (a "print server"; it can be another container).
  Add it in **Devices → Print servers**; Folio can then add printers to it
  once you connect a CUPS administrator login.
- For scanning: an eSCL/AirScan or WSD network scanner, or any scanner SANE
  supports that is visible inside the container.

## Install

```bash
git clone <repository-url> folio && cd folio
cp .env.example .env
nano .env          # Set SCAN_STUDIO_ADMIN_PASSWORD (12+ characters)
chmod 600 .env
docker compose up -d --build
```

Open `http://<host>:8870`, sign in as `admin` (or `SCAN_STUDIO_ADMIN_USER`),
then:

1. **Devices → Add a network scanner**: name, IP address, protocol (WSD or
   eSCL) and, if needed, port and endpoint path. Press **Test**.
2. **Devices → Print servers**: add the computer running CUPS and connect
   its administrator login. Then **Add a printer**, print a test page, and
   choose who may use it.
3. **Devices → Run checks** to confirm storage, printers and scanners.

### Portainer

The Compose file builds the image from source (`build: .`). Either run
`docker compose up -d --build` in the source directory and manage the
container from Portainer, or build the image first
(`docker build --build-arg FOLIO_EDITION=full -t folio-scan-studio:0.12.0-full .`), then paste `compose.yaml`
into a Portainer stack without the `build:` line and set the variables there.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FOLIO_EDITION` | `full` | `full` or `lite` (build setting; see Editions) |
| `SCAN_STUDIO_ADMIN_PASSWORD` | — (required) | First administrator's password, used only when no accounts exist |
| `SCAN_STUDIO_ADMIN_USER` | `admin` | First administrator's username |
| `CUPS_SERVERS` | empty | Optional: print servers to always use, e.g. `cups:631,192.168.1.10:631`. More can be added in Devices |
| `COOKIE_SECURE` | `false` | Set `true` when Folio is served over HTTPS |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Reverse proxies to trust for the visitor's address (IPs or networks) |
| `FOLIO_AIRSCAN_DISCOVERY` | `off` | `on` to also discover eSCL/WSD scanners by multicast |
| `FOLIO_RENDER_CONCURRENCY` | 2 (1 on one core) | Full-resolution page decodes allowed at once |
| `USER_PRINT_MAX_MB` | `256` | Upload size ceiling for signed-in users (64–2048) |
| `SCAN_TIMEOUT_SECONDS` / `SCAN_GRACE_SECONDS` | `900` / `30` | Maximum scan time; time allowed for a scanner to cancel cleanly |

All state lives in `./data` (accounts, documents, device registry). Back it
up before upgrading; see [CHANGELOG.md](CHANGELOG.md) for upgrade notes.

### Behind a reverse proxy

Serve Folio over HTTPS, set `COOKIE_SECURE=true`, and set
`FORWARDED_ALLOW_IPS` to the proxy's address or Docker network. Without it,
every visitor appears to come from the proxy, so sign-in lockouts and guest
limits are shared by everyone. Don't use `*` if port 8870 is also reachable
directly. **Devices → Run checks** reports whether the proxy is trusted.

## Troubleshooting

Start with **Devices → Run checks**. From the Docker host you can also run:

```bash
docker exec scan-studio scanimage -L                        # Scanners SANE can see
docker exec scan-studio scanimage -d 'DEVICE' --all-options  # A scanner's modes, sources, DPI
docker exec scan-studio lpstat -h CUPS_SERVER -p             # Printer queues on a CUPS server
docker exec scan-studio lpoptions -h CUPS_SERVER -p QUEUE -l # Sizes, trays, duplex for a queue
```

A printer option that CUPS does not report (a tray, an envelope size) cannot
be selected in Folio; fix it in the queue's driver settings in CUPS, then
press **Refresh devices**. A successful network test alone does not prove a
scanner can negotiate a scan session; use **Test** in Devices.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
docker run --rm -v "$PWD/frontend:/src:ro" node:22-alpine node --check /src/app.js
```

Tests simulate scanners and CUPS; they never need hardware or write outside
temporary directories. `requirements-dev.txt` includes the Full extras. To
test the Lite edition, run the suite inside its image (tests needing OpenCV
are skipped automatically):

```bash
docker build --build-arg FOLIO_EDITION=lite -t folio-scan-studio:lite .
docker run --rm --entrypoint sh -v "$PWD:/src:ro" -w /src folio-scan-studio:lite \
  -c 'pip install -q pytest httpx pypdf && python -m pytest -q -p no:cacheprovider'
``` Design decisions and direction are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
