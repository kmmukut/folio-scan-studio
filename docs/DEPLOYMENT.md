# Deployment

Folio runs as one Docker container. It needs:

- **Docker with Compose v2** on Linux (x86-64 or 64-bit ARM). Portainer
  works too.
- **Memory:** about 180 MB at rest, including the built-in print server
  (measured with the Full edition); previews and exports of large scans
  need a few hundred MB more while they run.
- **Nothing else for printing:** Folio has its own print server (CUPS with
  drivers for most printers) built in. See [Printing](#printing).
- **For scanning:** a network scanner that speaks eSCL (AirScan) or WSD,
  or a scanner SANE supports. See [Hardware](HARDWARE.md).

- [Docker Compose](#docker-compose)
- [Portainer](#portainer)
- [Raspberry Pi](#raspberry-pi)
- [Printing](#printing): AirPrint, CUPS's own pages, other CUPS servers
- [Email: invitations, password resets and notifications](#email-invitations-password-resets-and-notifications)
- [Sending documents to Paperless-ngx or a folder](#sending-documents-to-paperless-ngx-or-a-folder)
- [USB printers and scanners](#usb-printers-and-scanners)
- [Behind a reverse proxy](#behind-a-reverse-proxy)
- [Installing Folio as an app](#installing-folio-as-an-app)
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
(`id -u`, `id -g`) if they differ. The folder itself, and the print server's
`cups/` inside it, belong to root (see
[Configuration](CONFIGURATION.md#the-data-folder)); your user can still
read and add files through the `PGID` group.

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
FOLIO_EDITION=lite             # leaves out OCR and auto-straighten (~0.9 GB)
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
   pick the driver for your model. For a driverless IPP printer, Folio first
   asks the printer which formats it accepts. Driverless printing needs PDF
   or PWG raster; if the printer offers only Apple's AirPrint raster (some
   Samsung and older HP models), its pages can come out garbled. In that
   case Folio names the best-matching driver and offers to use it instead.

Its printers, drivers and settings are kept in `data/cups`.

### CUPS's own administration pages

Folio covers adding, pausing and removing printers. For anything else CUPS
can do, such as a printer's default options, classes or job limits, you can
turn on CUPS's own web pages: **Devices → Print servers → Built-in print
server → CUPS administration**. Choose a username and a password (at least
12 characters). Folio passes them to the print server, which creates the
login. Folio doesn't keep the password; the print server keeps only its
hash, in `data/cups`, so the login survives upgrades.

The pages are at `https://<host>:631/admin` (port 631 must be published, as
for AirPrint) and work over **HTTPS only**. The first time, the browser
warns about the print server's certificate, because CUPS makes its own
(self-signed). Plain HTTP is refused for administration before any password
is checked. **Turn off** removes the login.

Folio keeps working while CUPS's pages are on; it administers the print
server inside the container, with its own login.

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

## Email: invitations, password resets and notifications

With email set up, Folio can:
- **Invite people.** They get a link and choose their own password.
- **Offer "Forgot your password?"** on the sign-in page, for every account
  that has an email address, administrators included.
- **Send notifications.**

Set it up in **Devices → Email**:

1. **Folio's address**, as people open it (e.g. `https://folio.example.com`).
   Links in emails point there. Folio never takes the address from the
   incoming request, because anyone can fake that header and point a reset
   link at their own site. Alternatively, set `FOLIO_PUBLIC_URL`.
2. **The mail server**, its encryption and login, and the address emails
   come from:
   - With Gmail, Fastmail, Outlook and most other providers, use their SMTP
     server with STARTTLS on port 587 and an *app password* (made in the
     account's security settings), not your normal password.
   - A relay on your own network may need no login.
3. **Send a test email**, to check the settings.
4. **Add your own address** under **Your account** (click your initial,
   top right). Notifications for administrators go there.

**Invitations:** in **People & access**, add someone with their email
address and choose *They choose their own password*.
- Folio emails them a link that works once, within 48 hours.
- Without an address, or without email set up, Folio shows the link for you
  to send any way you like.
- **Copy a new invitation link** replaces an expired one.

**Forgot your password?** It's on by default once email is set up and
Folio's address is known; you can turn it off in Devices → Email.
- It sends a link that works once, within one hour.
- Using the link signs the account out everywhere else.
- The page answers the same whether or not an account exists, and requests
  are rate-limited.

**Notifications** (each can be switched off):
- **To administrators:**
  - toner or ink running low
  - a printer that needs attention (out of paper, a jam, a cover open)
  - someone accepting an invitation
- **To the person concerned:** their monthly page limit is used up.
- **Always sent:** a password change, to the account's owner.

Printers are checked every five minutes. Each problem is reported once, and
again only after it has cleared and come back. "Offline" isn't reported,
because printers that sleep at night would set it off every evening.

The SMTP password stays on the Folio server: it's never sent to browsers,
and it's included in backups like Folio's other logins.

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
5. If you use email, set Folio's address to the proxy's
   (`FOLIO_PUBLIC_URL=https://folio.example.com`, or in **Devices → Email**),
   so links in emails point there.

The proxy only serves Folio's web pages. Printing from phones (AirPrint)
and CUPS's own administration pages use port 631 directly, not the proxy.

Long exports and print jobs run in the background, so proxy timeouts don't
cut them off. Caching proxies are fine: Folio's scripts and styles have a
fingerprint in their address, so an upgrade is picked up at once. **Devices
→ Run checks** reports whether the proxy is trusted.

Folio is designed for a home or office network. If you make it reachable
from the internet, use HTTPS and strong passwords, and keep guest printing
off unless you need it.

## Installing Folio as an app

Folio can be installed on phones, tablets and computers like an app: its
own icon, its own window, no address bar.

- **Android (Chrome):** menu → **Add to home screen** → **Install**.
- **iPhone and iPad (Safari):** Share → **Add to Home Screen**.
- **Computers (Chrome, Edge):** the install icon at the right of the address
  bar.

Browsers offer a real install only when Folio is served over **HTTPS**, for
example behind a [reverse proxy](#behind-a-reverse-proxy). Opened as
`http://<address>:8870`, Folio works the same, but a home-screen icon is
only a bookmark that opens in the browser.

The app is Folio itself, from your server. It keeps nothing on the device:
without a connection to your network, it says that Folio can't be reached.
Upgrades reach it at once. On iPhones, the installed app has its own sign-in,
separate from Safari's.

## Backups and upgrades

**In Folio:** **Devices → Backup → Download a backup** gives you one file
with your documents, accounts, settings, print statistics, and the built-in
printers with their drivers. **Restore from a backup** puts it all back,
also on a new installation: the built-in printers are set up again from the
backup. What a restore replaces is kept in `data/.before-restore`. The file
holds your accounts and your print-server and email logins, so keep it
somewhere safe.

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

With `FOLIO_VERSION=0.20` this picks up 0.20.x fixes. To move to a new
minor version, change `FOLIO_VERSION` (e.g. to `0.21`). To go back, restore
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
