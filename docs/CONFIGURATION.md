# Configuration

Folio is configured in two places:

- **Environment variables**, read when the container starts: in `.env` next
  to `compose.yaml`, or as stack variables in Portainer. They cover what has
  to be known before anyone signs in. [.env.example](../.env.example) lists
  them all with comments.
- **In Folio itself**, by an administrator: print servers, printers,
  scanners, accounts, guest printing and statistics. These are stored in the
  data folder and survive upgrades.

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `SCAN_STUDIO_ADMIN_PASSWORD` | (required) | Password of the first administrator, created on first start when no accounts exist. 12+ characters. Change it later in Folio; editing this afterwards has no effect. |
| `SCAN_STUDIO_ADMIN_USER` | `admin` | Username of that first administrator. |
| `FOLIO_EDITION` | `full` | `full` or `lite`. Picks the image (`…-full` / `…-lite`), or what is built from source. See [Editions](../README.md#images-and-editions). |
| `FOLIO_VERSION` | `0.15` | Image version. `0.15` receives 0.15.x fixes on `docker compose pull`; `0.15.0` stays exactly there. |
| `FOLIO_PORT` | `8870` | Port on the host that Folio is reachable on. |
| `TZ` | `UTC` | Time zone, e.g. `Europe/Berlin`. Decides which day and month a print counts in for statistics. |
| `COOKIE_SECURE` | `false` | Set `true` when Folio is served over HTTPS (behind a reverse proxy). Sign-in cookies are then only sent over HTTPS. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Reverse proxies whose `X-Forwarded-For` header is trusted, as IPs or networks (`172.19.0.0/16`). See [Reverse proxy](DEPLOYMENT.md#behind-a-reverse-proxy). |
| `FOLIO_BUILTIN_CUPS` | `on` | Folio's built-in print server. `off` if you only use CUPS on other computers. See [Printing](DEPLOYMENT.md#printing). |
| `FOLIO_AIRPRINT_PORT` | `631` | The host port that reaches the built-in print server, used in AirPrint announcements. Change it only if you publish 631 as another port. |
| `CUPS_SERVERS` | empty | CUPS on other computers that is always used, e.g. `192.168.1.10:631`. Optional: more are added in **Devices → Print servers**. Servers listed here can only be removed here. |
| `USER_PRINT_MAX_MB` | `256` | Largest file a signed-in person may upload to print (64–2048). Guests are always limited to 10 MB and 20 pages. |
| `FOLIO_AIRSCAN_DISCOVERY` | `off` | `on` also finds eSCL/WSD scanners by multicast. That usually needs `network_mode: host`; adding scanners by address works without it. |
| `SCAN_TIMEOUT_SECONDS` | `900` | Longest a single scan may run. |
| `SCAN_GRACE_SECONDS` | `30` | How long a cancelled scan gets to stop cleanly before it is ended. |
| `FOLIO_RENDER_CONCURRENCY` | 2 (1 on one core) | Full-resolution page decodes allowed at once. Lower it on hosts with little memory. |

Changing a variable takes effect after `docker compose up -d` (which
recreates the container); data is kept.

## In Folio

| Where | What |
|---|---|
| **Devices → Print servers** | The built-in print server, and CUPS on other computers with their administrator logins. |
| **Devices → Add a printer** | Create a printer: driverless (IPP Everywhere) or with a driver chosen by model. |
| **Devices → Printers** | Default printer, who may use each printer (members, print-only accounts, guests), toner and parts levels, test page, pause, remove, jobs. |
| **Devices → Scanners** | Network scanners by address (WSD or eSCL), and scanners SANE finds. |
| **Devices → Run checks** | Read-only System check of storage, memory, proxy, print servers and scanners. |
| **People & access** | Accounts: administrator, member (own documents and printing), print-only. |
| **Print desk** | Guest printing on or off. |
| **Statistics** | Whether print statistics are kept and for how long, and whether prints sent straight to the print server (from phones and computers) are counted too. |

## The data folder

Everything Folio keeps is in the folder mounted at `/data` (`./data` next
to `compose.yaml` by default). Back it up as a whole; see
[Backups](DEPLOYMENT.md#backups-and-upgrades). Folio keeps its files private
to itself; other users on the host may pass through the folder but not list
or read it.

| Path | Contents |
|---|---|
| `access.sqlite3` | Accounts (passwords as salted PBKDF2 hashes), sessions, guest setting. |
| `cups/` | The built-in print server: its printers, drivers, settings, job history, and the administrator login Folio created for itself. |
| `cups-admin.json` | CUPS administrator logins for other print servers (readable by root only, never sent to browsers). |
| `devices.json` | Scanners, print servers, printer settings and defaults. |
| `managed-airscan.conf` | Scanner configuration Folio writes for SANE; regenerated from `devices.json`. |
| `print-log.sqlite3` | Print statistics: one row per job, no document names. Safe to delete. |
| `jobs/` | Documents: original scans (never modified), edits, and a disposable preview cache. |
| `print-previews/`, `tasks/` | Temporary files, cleared automatically. |
