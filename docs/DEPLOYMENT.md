# Deployment

Folio runs as one Docker container. It needs:

- **Docker with Compose v2** on Linux (x86-64 or 64-bit ARM). Portainer
  works too.
- **Memory:** about 60 MB at rest; previews and exports of large scans
  peak around 400 MB.
- **Nothing else for printing:** Folio has its own print server (CUPS with
  drivers for most printers) built in. See [Printing](#printing).
- **For scanning:** a network scanner that speaks eSCL (AirScan) or WSD,
  or a scanner SANE supports. See [Hardware](HARDWARE.md).

- [Docker Compose](#docker-compose)
- [Portainer](#portainer)
- [Raspberry Pi](#raspberry-pi)
- [Printing](#printing)
- [Sending documents to Paperless-ngx or a folder](#sending-documents-to-paperless-ngx-or-a-folder)
- [USB printers and scanners](#usb-printers-and-scanners)
- [Behind a reverse proxy](#behind-a-reverse-proxy)
- [Backups and upgrades](#backups-and-upgrades)
- [Building from source](#building-from-source)

## Docker Compose

```bash
mkdir folio && cd folio
curl -fsSLO https://raw.githubusercontent.com/kmmukut/folio-scan-studio/main/compose.yaml
curl -fsSL https://raw.githubusercontent.com/kmmukut/folio-scan-studio/main/.env.example -o .env
nano .env          # optional: TZ, FOLIO_EDITION, PUID/PGID
docker compose up -d
```

Open `http://<host>:8870` and create the administrator account. For safety
that is only possible in the first 15 minutes after Folio starts; if you
miss it, `docker compose restart` opens the window again. (Or set
`SCAN_STUDIO_ADMIN_PASSWORD` in `.env` to create the administrator at the
first start instead.) Your data is in `./data`. Every setting is described
in [Configuration](CONFIGURATION.md).

Folio runs as an ordinary user, `PUID`/`PGID` (1000 by default), so the
files in `./data` belong to that user on the host. Set them to your own ids
(`id -u`, `id -g`) if they differ.

## Portainer

1. **Stacks → Add stack**, name it `folio`, choose **Web editor** and paste
   [compose.yaml](../compose.yaml).
2. Change `./data:/data` to an absolute path on the host, e.g.
   `/opt/folio/data:/data`. Portainer resolves relative paths inside its
   own folders, which makes backups hard to find.
3. Under **Environment variables** add `TZ`, plus any others from
   [.env.example](../.env.example) you want to change.
4. **Deploy the stack**, open Folio and create the administrator account
   within 15 minutes.
   To upgrade later, open the stack and choose **Update the stack** with
   **Re-pull image** switched on.

## Raspberry Pi

Use a **64-bit** OS (Raspberry Pi OS 64-bit or Ubuntu Server for ARM); the
images are for 64-bit ARM only. A Pi 4 or 5 with 2 GB or more is
comfortable; a Pi 3 works but has little memory to spare.

```bash
curl -fsSL https://get.docker.com | sh       # Docker's install script
sudo usermod -aG docker "$USER"              # then log out and back in
```

Then follow [Docker Compose](#docker-compose) with, in `.env`:

```bash
FOLIO_EDITION=lite             # leaves out OCR and auto-straighten (~890 MB)
FOLIO_RENDER_CONCURRENCY=1     # one full-size page decode at a time
```

Raspberry Pi OS runs Avahi, so you can let phones find your printers; see
[AirPrint](#airprint-phones-and-computers). The arm64 images are built and
their tests run on 64-bit ARM for every release, but the maintainers have
not yet run Folio on a real Raspberry Pi.
If you do, please share how it went in a
[hardware report](https://github.com/kmmukut/folio-scan-studio/issues/new?template=hardware_report.yml).

## Printing

Folio's container includes a **built-in print server**: CUPS, the standard
print server on Linux and macOS, with the printer drivers Debian ships for
thousands of models. Folio sets it up and connects to it by itself, so
**Devices → Add a printer** works straight away:

1. Choose the *Built-in* print server.
2. Type the printer's IP address, or search for it.
3. Choose *Driverless* for most network printers made since about 2015, or
   pick the driver for your model.

Its printers, drivers and settings are kept in `data/cups`.

### AirPrint: phones and computers

To let phones, tablets and computers find the built-in printers and print
to them without Folio (AirPrint, IPP), publish the print server's port and
let the host announce the printers. In `compose.yaml`:

```yaml
    ports:
      - "8870:8870"
      - "631:631"                                  # the built-in print server
    volumes:
      - ./data:/data
      - /etc/avahi/services:/etc/avahi/services    # announce its printers
```

Folio then writes an announcement for each printer into the host's Avahi
folder, and the host's Avahi advertises it with the host's own address.
Raspberry Pi OS and most Linux desktops run Avahi already; on a server,
install it with `sudo apt install avahi-daemon`. If you publish a different
host port (e.g. `"6310:631"`), set `FOLIO_AIRPRINT_PORT=6310`.

Announcements only reach the network the host is on. If your phones are on
another network or VLAN, your router has to repeat them (look for an "mDNS
repeater" or "multicast DNS" setting) and allow traffic to the host's port.

Prints sent this way don't go through Folio, so they aren't in Folio's
statistics unless you switch on **Statistics → Also count prints sent
straight to the print server**.

### Other CUPS servers

You can also use CUPS on other computers, alongside the built-in one or
instead of it (`FOLIO_BUILTIN_CUPS=off`). On that computer:

```bash
sudo cupsctl --share-printers --remote-admin --remote-any
sudo usermod -aG lpadmin <user>    # the account Folio will log in with
```

That lets CUPS accept connections from the network and lets that user
administer printers. In Folio, **Devices → Print servers → Add another print
server** with the computer's address (port 631 is assumed), then connect the
login. Folio checks the server answers before saving it, and uses an
encrypted connection when CUPS asks for one. Printers Folio creates are
always shared, because CUPS only accepts jobs from other computers for
shared printers.

## Sending documents to Paperless-ngx or a folder

Finished documents can be sent from the workbench (**Send**, next to
Export) to destinations an administrator sets up under **Devices →
Destinations**.

**Paperless-ngx:** enter its address (e.g. `http://192.168.1.20:8000`) and an
API token (in Paperless: your profile → API token), press **Check**, and
choose tags every document sent there should get. Folio uploads the PDF with
its title; Paperless reads the text and files it as usual. For a
self-signed HTTPS certificate, untick certificate checking.

**A folder:** mount the host folder into the container under
`/destinations`, then add it by name:

```yaml
    volumes:
      - ./data:/data
      - /srv/scans:/destinations/nas        # e.g. a NAS share mounted on the host
```

The user Folio runs as (`PUID`) must be allowed to write to it. Files are
named `2026-10-27 Title.pdf` and appear complete at once, so a folder
watcher (such as Paperless's consume folder) never sees half a file.

## USB printers and scanners

USB support is a goal, but the maintainers have no USB devices to test
with, so these instructions are untested. Please report what works.

Give Folio's container the host's USB devices, in `compose.yaml`:

```yaml
    devices:
      - /dev/bus/usb:/dev/bus/usb
```

- **USB printer:** plug it into the host, restart Folio, then **Devices →
  Add a printer → Search** on the built-in print server.
- **USB scanner:** check `docker exec scan-studio scanimage -L`, then attach
  the scanner in **Devices**. Folio runs as an ordinary user; if the scanner
  isn't listed because only root may open it, set `PUID=0`.

A device plugged in after Folio started may need a restart of the container
to appear.

## Behind a reverse proxy

To serve Folio over HTTPS (Nginx Proxy Manager, Caddy, Traefik, nginx):

1. Proxy to `http://<folio-host>:8870`. No WebSocket support is needed.
2. Set `COOKIE_SECURE=true`.
3. Set `FORWARDED_ALLOW_IPS` to the proxy's address or Docker network
   (e.g. `172.19.0.0/16`). Without it every visitor seems to come from the
   proxy, so sign-in lockouts and guest limits are shared by everyone. Don't
   use `*` if port 8870 is also reachable directly.
4. Allow uploads as large as `USER_PRINT_MAX_MB` (256 MB by default), e.g.
   `client_max_body_size 256m;` in nginx.

Long exports and print jobs run in the background, so proxy timeouts don't
cut them off. Caching proxies are fine: Folio's scripts and styles have a
fingerprint in their address, so an upgrade is picked up at once. **Devices
→ Run checks** reports whether the proxy is trusted.

Folio is designed for a home or office network. If you make it reachable
from the internet, use HTTPS and strong passwords, and keep guest printing
off unless you need it.

## Backups and upgrades

**In Folio:** **Devices → Backup → Download a backup** gives you one file
with your documents, accounts, settings, print statistics, and the built-in
printers with their drivers. **Restore from a backup** puts it all back,
also on a new installation: the built-in printers are set up again from the
backup. What a restore replaces is kept in `data/.before-restore`. The file
holds your accounts and print-server logins, so keep it somewhere safe.

**From the host:** everything Folio keeps is in the data folder.

```bash
docker compose stop
sudo tar -czf folio-data-$(date +%F).tgz data   # sudo: the print server's files belong to root
docker compose start
```

To upgrade, read the upgrade notes in the [changelog](../CHANGELOG.md),
make a backup, then:

```bash
docker compose pull
docker compose up -d
```

With `FOLIO_VERSION=0.17` this picks up 0.17.x fixes. To move to a new
minor version, change `FOLIO_VERSION` (e.g. to `0.18`). To go back, restore
the backup and set the previous version; newer versions may change the
data folder in ways older ones don't understand.

## Building from source

```bash
git clone https://github.com/kmmukut/folio-scan-studio.git && cd folio-scan-studio
cp .env.example .env && nano .env
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

`FOLIO_EDITION` in `.env` picks what is built. See
[CONTRIBUTING.md](../CONTRIBUTING.md) for running the tests.
