# Configuration

Folio is configured in two places:

- **Environment variables**, read when the container starts: in `.env` next
  to `compose.yaml`, or as stack variables in Portainer. They cover what has
  to be known before anyone signs in. [.env.example](../.env.example) lists
  them all with comments.
- **In Folio itself**, by an administrator: print servers, printers,
  scanners, destinations (Paperless-ngx, folders), email, accounts and
  invitations, guest printing, page quotas, job limits and statistics. These are stored
  in the data folder and survive upgrades.

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `SCAN_STUDIO_ADMIN_PASSWORD` | empty | Empty: create the first administrator in the browser (within 15 minutes of starting). Set (12+ characters): create it at the first start. Change it later in Folio; editing this afterwards has no effect. |
| `SCAN_STUDIO_ADMIN_USER` | `admin` | Username of that first administrator, when created from the setting. |
| `PUID` / `PGID` | `1000` / `1000` | The user and group Folio runs as, and who owns its files on the host. `PUID=0` runs it as root (some USB scanners need that). |
| `FOLIO_EDITION` | `full` | `full` or `lite`. Picks the image (`…-full` / `…-lite`), or what is built from source. See [Editions](../README.md#images-and-editions). |
| `FOLIO_VERSION` | `0.22` | Image version. `0.22` receives 0.22.x fixes on `docker compose pull`; `0.22.0` stays exactly there. |
| `FOLIO_PORT` | `8870` | Port on the host that Folio is reachable on. |
| `TZ` | `UTC` | Time zone, e.g. `Europe/Berlin`. Decides which day and month a print counts in for statistics. |
| `COOKIE_SECURE` | `false` | Set `true` when Folio is served over HTTPS (behind a reverse proxy). Sign-in cookies are then only sent over HTTPS. |
| `FOLIO_PUBLIC_URL` | empty | Folio's address as people open it, e.g. `https://folio.example.com`, for links in invitation and password emails. Can also be set in **Devices → Email**, which takes precedence. See [Email](DEPLOYMENT.md#email-invitations-password-resets-and-notifications). |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Reverse proxies whose `X-Forwarded-For` header is trusted, as IPs or networks (`172.19.0.0/16`). See [Reverse proxy](DEPLOYMENT.md#behind-a-reverse-proxy). |
| `FOLIO_BUILTIN_CUPS` | `on` | Folio's built-in print server. `off` if you only use CUPS on other computers. See [Printing](DEPLOYMENT.md#printing). |
| `FOLIO_AIRPRINT_PORT` | `631` | The host port that reaches the built-in print server, used in AirPrint announcements. Change it only if you publish 631 as another port. |
| `CUPS_SERVERS` | empty | CUPS on other computers that is always used, e.g. `192.168.1.10:631`. Optional: more are added in **Devices → Print servers**. Servers listed here can only be removed here. |
| `USER_PRINT_MAX_MB` | `256` | Largest file a signed-in person may upload to print (64–2048). Guests are always limited to 10 MB and 20 pages. |
| `FOLIO_AIRSCAN_DISCOVERY` | `off` | `on` also finds eSCL/WSD scanners by multicast. That usually needs `network_mode: host`; adding scanners by address works without it. |
| `FOLIO_AIRSCAN_PORT` | `8090` | The host port that reaches scanners shared as AirScan, used in their announcements. Change it only if you publish 8090 as another port. |
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
| **Devices → Destinations** | Paperless-ngx servers and folders documents can be sent to from the workbench. |
| **Devices → Backup** | Download a backup of everything, or restore one. |
| **Workbench → Getting started** | A checklist of the first steps; hide it when you're done. |
| **People & access** | Accounts: administrator, member (own documents and printing), print-only; monthly page limits (a default, and per person). |
| **Print desk** | Guest printing on or off. |
| **Statistics** | Whether print statistics are kept and for how long, and whether prints sent straight to the print server (from phones and computers) are counted too. |

## The data folder

Everything Folio keeps is in the folder mounted at `/data` (`./data` next
to `compose.yaml` by default). Back it up as a whole; see
[Backups](DEPLOYMENT.md#backups-and-upgrades). Folio keeps its files private
to itself; other users on the host may pass through the folder but not list
or read it.

The files in it belong to `PUID`. The folder itself belongs to root, with
the `PGID` group allowed to add files and the sticky bit set (`drwxrwx--t`).
Each owner may then rename or remove only their own files. Folio keeps full
use of its own, but can't swap the print server's `cups/` (root's) for a
folder of its own. Folio's web process is the part exposed to the network;
the print server's supervisor runs as root. With `PUID=0`, or with the
built-in print server off, the folder belongs to `PUID` as before.

| Path | Contents |
|---|---|
| `access.sqlite3` | Accounts (passwords as salted PBKDF2 hashes, email addresses), sessions, invitation and password links (only as hashes), settings including email (with the SMTP password, never sent to browsers). |
| `cups/` | The built-in print server (root's): its printers, drivers, settings, job history, the administrator login Folio created for itself, and, if CUPS administration is on, that login's password hash. |
| `cups-admin.json` | CUPS administrator logins for other print servers (readable by root only, never sent to browsers). |
| `devices.json` | Scanners, print servers, printer settings, defaults, and destinations (including Paperless API tokens, never sent to browsers). |
| `managed-airscan.conf` | Scanner configuration Folio writes for SANE; regenerated from `devices.json`. |
| `airscan/` | AirSane's settings for the scanners shared as AirScan, and what to announce; regenerated. |
| `machine-id` | This installation's own ID, which shared scanners' AirScan addresses are made from, so they stay the same across updates. Not in Folio's backups: Folio restored on a new computer gets a new one. |
| `print-log.sqlite3` | Print statistics: one row per job, no document names. Safe to delete. |
| `jobs/` | Documents: original scans (never modified), edits, and a disposable preview cache. |
| `print-previews/`, `tasks/` | Temporary files, cleared automatically. |
