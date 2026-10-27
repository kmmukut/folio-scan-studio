# Deployment

Folio runs as one Docker container. It needs:

- **Docker with Compose v2** on Linux (x86-64 or 64-bit ARM). Portainer
  works too.
- **Memory:** about 60 MB at rest; previews and exports of large scans
  peak around 400 MB.
- **A print server:** a computer running CUPS that Folio sends jobs to. It
  can be the same machine, another container, or another computer. See
  [A CUPS server](#a-cups-server).
- **For scanning:** a network scanner that speaks eSCL (AirScan) or WSD,
  or a scanner SANE supports. See [Hardware](HARDWARE.md).

- [Docker Compose](#docker-compose)
- [Portainer](#portainer)
- [Raspberry Pi](#raspberry-pi)
- [A CUPS server](#a-cups-server)
- [USB printers and scanners](#usb-printers-and-scanners)
- [Behind a reverse proxy](#behind-a-reverse-proxy)
- [Backups and upgrades](#backups-and-upgrades)
- [Building from source](#building-from-source)

## Docker Compose

```bash
mkdir folio && cd folio
curl -fsSLO https://raw.githubusercontent.com/kmmukut/folio-scan-studio/main/compose.yaml
curl -fsSL https://raw.githubusercontent.com/kmmukut/folio-scan-studio/main/.env.example -o .env
nano .env          # at least SCAN_STUDIO_ADMIN_PASSWORD and TZ
chmod 600 .env     # it holds a password
docker compose up -d
```

Open `http://<host>:8870` and sign in as `admin` with that password. Your
data is in `./data`. Every setting is described in
[Configuration](CONFIGURATION.md).

## Portainer

1. **Stacks → Add stack**, name it `folio`, choose **Web editor** and paste
   [compose.yaml](../compose.yaml).
2. Change `./data:/data` to an absolute path on the host, e.g.
   `/opt/folio/data:/data`. Portainer resolves relative paths inside its
   own folders, which makes backups hard to find.
3. Under **Environment variables** add at least
   `SCAN_STUDIO_ADMIN_PASSWORD` and `TZ`, plus any others from
   [.env.example](../.env.example) you want to change.
4. **Deploy the stack**. To upgrade later, open the stack and choose
   **Update the stack** with **Re-pull image** switched on.

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
FOLIO_EDITION=lite             # leaves out OCR and auto-straighten (~660 MB)
FOLIO_RENDER_CONCURRENCY=1     # one full-size page decode at a time
```

The arm64 images are built and their tests run on 64-bit ARM for every
release, but the maintainers have not yet run Folio on a real Raspberry Pi.
If you do, please share how it went in a
[hardware report](https://github.com/kmmukut/folio-scan-studio/issues/new?template=hardware_report.yml).

## A CUPS server

Folio doesn't drive printers itself: it sends jobs to CUPS, the standard
print server on Linux and macOS, and manages CUPS printers over its network
API. You need one CUPS server that Folio can reach on port 631.

**Option 1: CUPS next to Folio.** For hosts that don't run CUPS yet,
[compose.cups.yaml](../compose.cups.yaml) adds a CUPS container (the
community image `anujdatar/cups`):

```bash
curl -fsSLO https://raw.githubusercontent.com/kmmukut/folio-scan-studio/main/compose.cups.yaml
nano .env          # set CUPS_ADMIN_PASSWORD
docker compose -f compose.yaml -f compose.cups.yaml up -d
```

In Folio, **Devices → Print servers** shows `cups:631`; connect it with
`CUPS_ADMIN` and `CUPS_ADMIN_PASSWORD`, then **Add a printer**.

**Option 2: an existing CUPS server.** On that computer:

```bash
sudo cupsctl --share-printers --remote-admin --remote-any
sudo usermod -aG lpadmin <user>    # the account Folio will log in with
```

That lets CUPS accept connections from the network, and lets that user
administer printers. In Folio, **Devices → Print servers → Add a print
server** with the computer's address (port 631 is assumed), then connect the
login. Folio checks the server answers before saving it, and uses an
encrypted connection when CUPS asks for one.

Printers Folio creates are always shared, because CUPS only accepts jobs
from other computers for shared printers. Folio itself contains no printer
drivers: driverless printers (IPP Everywhere, AirPrint) need none, and older
printers need a driver installed on the CUPS server.

## USB printers and scanners

USB support is a goal, but the maintainers have no USB devices to test
with, so these instructions are untested. Please report what works.

- **USB printer:** connect it to the computer running CUPS. With the CUPS
  container from Option 1, uncomment the `devices:` lines in
  `compose.cups.yaml` so CUPS can see `/dev/bus/usb`. The printer then
  appears under **Devices → Add a printer → Search**.
- **USB scanner:** Folio's container runs SANE, so it needs the device:

  ```yaml
  services:
    scan-studio:
      devices:
        - /dev/bus/usb:/dev/bus/usb
  ```

  Then check `docker exec scan-studio scanimage -L` and attach the scanner in
  **Devices**.

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

Everything Folio keeps is in the data folder. To back it up:

```bash
docker compose stop
tar -czf folio-data-$(date +%F).tgz data
docker compose start
```

To upgrade, read the upgrade notes in the [changelog](../CHANGELOG.md),
make a backup, then:

```bash
docker compose pull
docker compose up -d
```

With `FOLIO_VERSION=0.14` this picks up 0.14.x fixes. To move to a new
minor version, change `FOLIO_VERSION` (e.g. to `0.15`). To go back, restore
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
