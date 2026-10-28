# Changelog

All notable changes to Folio. Versions follow `MAJOR.MINOR.PATCH`; until 1.0,
minor versions may add features and change defaults (always with upgrade notes).
Sections for 0.9.5 and earlier are carried over from the release READMEs.

## 0.20.1 — Install it like an app; documentation brought up to date

**Folio can be installed on phones, tablets and computers**, with its own
icon (Folio's "F.") and its own window without an address bar:
- **Android (Chrome):** menu → Add to home screen.
- **iPhone and iPad (Safari):** Share → Add to Home Screen.
- **Computers:** the install icon in Chrome's or Edge's address bar.

Browsers offer this only over HTTPS (for example behind a reverse proxy).
Over plain `http://`, Folio works as before.

The installed app keeps nothing on the device and needs no updating.
Without a connection to your network, it says that Folio can't be reached
and offers to try again. The browser tab now shows Folio's icon too.

**Documentation:**
- **README:** the images table still named 0.15; it now names 0.20, with
  the measured download sizes.
- **Deployment guide:**
  - memory use with the print server (about 180 MB at rest)
  - email and port 631 behind a reverse proxy
  - installing as an app
- **Hardware list:** the driverless check, HP scanning through eSCL, and
  the maintainer's Samsung (use the PXL driver; driverless garbles pages).
- **Contributing guide:** the image test scripts, and that no administrator
  password is needed.
- A new test fails if the documentation names a version other than the
  current one.

### Verified
- **Tests:** 244 on the host. Inside the images, on amd64: Full 246,
  Lite 243 with 3 skipped. They check the manifest, every icon's size, the page's
  links to them, and that the service worker stores nothing.
- **In headless Chrome** (on the computer Folio runs on, which browsers
  treat like HTTPS):
  - the service worker takes charge of the page
  - Chrome reports no manifest errors and no reasons it couldn't install
  - offline, the "can't be reached" page appears (checked at phone width),
    and *Try again* returns to the sign-in page once back online
- **Not verified:**
  - installing on a real phone, and iPhone home-screen behaviour (no
    device here)
  - the arm64 images (built and tested by the release workflow)

### Upgrading
Nothing is required. To install Folio, open it over HTTPS and use the
browser's install option.

## 0.20.0 — Email: invitations, password resets, notifications

Folio can now send email through any SMTP server: your provider (Gmail,
Fastmail and the like, with an app password) or a relay of your own. Set
it up in **Devices → Email**, then **Send a test email**.

**Invitations.** Under People & access, add someone with their email
address. Folio emails them a link to choose their own password, so nobody
has to pass a password along.
- The link works once, within 48 hours.
- Without email, Folio shows the link for you to send any way you like.
- People who haven't accepted yet are marked *Invited*.
- **Copy a new invitation link** replaces an expired one.
- **I set a password now** still works as before.

**"Forgot your password?"** on the sign-in page, on by default once email
is set up, for every account with an email address, administrators
included.
- The link works once, within one hour, and using it signs the account out
  everywhere else.
- The page answers the same whether or not the account exists.
- Requests are rate-limited.
- It can be turned off in Devices → Email.

**Your account** (click your initial, top right): everyone can now set
their own email address and change their own password. Until now, only an
administrator could change a password.

**Notifications** (each can be switched off):
- **To administrators:**
  - toner or ink running low
  - a printer needing attention (out of paper, a jam, a cover open)
  - someone accepting an invitation
- **To the person concerned:**
  - when their monthly page limit is used up
  - always, when their password is changed

Printers are checked every five minutes. Each problem is emailed once, and
again only after it clears and comes back; a restart doesn't repeat
anything.

**Security:**
- Links carry their token after `#`, which browsers never send to servers,
  so tokens stay out of proxy logs.
- Only a hash of each token is stored, and backups leave unused links out.
- Links point to Folio's address as set in Devices → Email (or
  `FOLIO_PUBLIC_URL`), never to one taken from the request, which anyone
  can fake.
- The SMTP password is never sent to browsers.

### Fixed
**Saving a person could set their monthly page limit to 0 (since 0.18).**
In People & access, everyone on the *Default* limit was shown as having
*Their own* limit with no number. Saving any change to them, even an email
address, set their limit to 0 pages, so they could no longer print. They
now show *Default*, and saving keeps it.

### Verified
- **Tests:** 240 on the host. Inside the images, on amd64: Full 242,
  Lite 239 with 3 skipped. A fake mail server receives real SMTP (plain, STARTTLS and
  SSL, with a login). The tests cover:
  - the test email, and the reasons shown for a wrong password, a refused
    address or an untrusted certificate
  - invitations by email and by link; choosing a password; a link working
    only once
  - "Forgot your password?" giving the same answer for unknown accounts;
    reset links expiring after an hour; rate limits; tokens stored only as
    hashes; backups without links
  - your own address and password, and the emails about password changes
  - notifications sent once, re-armed after clearing, "offline" ignored,
    and off when switched off
  - the five-minute printer check against the fake CUPS (real IPP)
  - page-limit emails, once a month
  - System check and the setup checklist
- **End to end, in headless Chrome, with Folio's image and a real mail
  catcher (Mailpit, STARTTLS required, with a login):**
  - setting up email and sending the test
  - inviting someone by email, opening the emailed link (also at phone
    width), choosing a password and being signed in
  - the "joined" email reaching the administrator
  - "Forgot your password?" by email address, then the reset and the
    "password changed" email
  - a used link being refused
  - the page-limit fix
- **Not verified:**
  - real mail providers (Gmail, Outlook): only Mailpit and the test server
  - a real printer running out of paper or toner: its state was simulated
    over IPP
  - the arm64 images (built and tested by the release workflow)

### Upgrading
Nothing is required. Existing accounts get an empty email address and work
as before. To use email, open **Devices → Email**, and add your own
address under **Your account**.

If you saved a person's settings on 0.18 or 0.19 and they can't print
("you have 0 of your 0 pages left"), set their page limit back to
*Default* in People & access.

## 0.19.0 — CUPS administration from Folio

**CUPS's own administration pages, with a login you choose in Folio.**
Folio covers adding, pausing and removing printers. For everything else
CUPS can do (default options, classes, job limits), go to **Devices → Print
servers → Built-in print server → CUPS administration**:
- Choose a username and a password of at least 12 characters, and the
  pages open at `https://<host>:631/admin`.
- **HTTPS only:** CUPS refuses administration over plain HTTP before any
  password is checked. The first time, your browser warns about the print
  server's own (self-signed) certificate.
- **Folio doesn't keep the password.** The print server keeps only its
  hash, in `data/cups`, so the login survives upgrades.
- **Change login** and **Turn off** are in the same place.
- **Off by default.** Port 631 must be published, as for AirPrint.

**A check before adding a printer as driverless.** Driverless printing
needs the printer to accept PDF or PWG raster. Some printers offer only
Apple's AirPrint raster, which can come out as garbled pages (seen with a
Samsung M2885FW). Folio now asks an IPP printer what it accepts before
adding it. If PDF and PWG raster are both missing, Folio says so and offers
the best-matching driver instead.

**Better first choice of driver.** When several drivers match a printer
equally, the one named after the printer's own model now comes first. That
is usually the manufacturer's own driver: "Samsung M288x Series PXL" for an
M288x, rather than Foomatic's "lj5gray".

### Security
**Folio could have replaced the print server's settings (since 0.16).**
Folio's web process runs as an ordinary user, but it owned the data folder.
It could therefore have renamed `data/cups` and put a folder of its own in
its place, which the print server's supervisor and CUPS, both running as
root, would then have used. Nothing suggests this was ever exploited; it
needed Folio's web process to be compromised first.

The data folder itself now belongs to root:
- Folio's group (`PGID`) may add files, and the sticky bit is set, so each
  owner may rename or remove only their own files.
- Folio keeps full use of its files but can't touch `cups/`.
- At start, the print server takes back anything in `cups/` that isn't
  root's and removes links there.

### Fixed
The real-CUPS tests waited for CUPS with `lpstat -r`, which reports success
even when CUPS isn't running. They now check what it prints. The earlier
test passed only because of timing.

### Verified
- **Tests:** 224 on the host. Inside the images, on amd64: Full 226, Lite 223 with 3 skipped.
  They cover:
  - turning the login on, changing it, a restart and turning it off
  - refusing system accounts, short passwords, links and altered stored
    logins
  - deleting a password request the print server doesn't pick up
  - administrators only
  - the driverless check and driver suggestion
- **With a real CUPS, inside the image:**
  - other computers can't administer while it's off
  - plain HTTP is refused both through `/admin` and through IPP directly
  - over HTTPS the login works and a wrong password doesn't
  - Folio's own local access keeps working
  - the login comes back after a restart, and turning it off removes it
- **The start-up check (both images):**
  - `/data` is root's with mode 1771
  - Folio can't move `cups/` but can write its own files
  - CUPS administration turned on through Folio's API answers 426 over HTTP
    and 200 over HTTPS
- **In headless Chrome:** the whole flow (a mismatched password, turning on,
  the link, changing, turning off), also at phone width (375 px).
- **The driverless check against the maintainer's Samsung M2885FW:** only
  asked what it accepts, nothing was printed. The check flagged it, and Folio
  offered Samsung's own PXL driver.
- **Upgrading from 0.18:** a data volume with a printer, plus a file in
  `cups/` belonging to the host user, started on 0.19. The printer was kept,
  the file taken back by root, and Folio still runs as its own user.
- **Not verified:** the arm64 images (built and tested by the release
  workflow).

### Upgrading
Nothing is required. On the first start, the data folder itself becomes
root's (`root:<PGID>`, `drwxrwx--t`). Everything in it stays yours.
Host scripts that change the folder itself (not its contents) now need
`sudo`. With `PUID=0`, or the built-in print server off, nothing changes.

If a printer you added as driverless prints garbled pages, remove it and
add it again: Folio now offers a driver.

## 0.18.1 — Scanning works again

**Fixed: scanners showed as offline from 0.15 on.** The HP printer drivers
added with the built-in print server in 0.15 come with HP's scanner
plug-in for SANE. In a container, where there is no D-Bus, that plug-in
crashes every scanner listing, so Folio could see no scanner at all.

The plug-in is now switched off in the image, and all the printer drivers
stay: 17,650 models, 7,141 of them HP. HP network scanners keep working
through eSCL (AirScan), like other network scanners.

Folio's tests simulated `scanimage`, so they couldn't notice. Two checks
now run against the real one:
- a test inside each image confirms listing scanners works
- the CI start-up check lists scanners as Folio's own user

### Verified
- **With the maintainer's scanner settings:** the 0.18 image aborts; the
  0.18.1 image lists the Samsung M2885FW (WSD). The scanner was only asked
  what it is; nothing was scanned.
- **Drivers:** still 17,650.
- **Tests:** the full suite runs inside both images, and the start-up check
  passes for both.

### Upgrading
Re-pull the image (`0.18` receives this fix). Until then, this restores
scanning in a running 0.18 container until it is recreated:
`docker exec scan-studio sed -i 's/^hpaio/#hpaio/' /etc/sane.d/dll.d/hplip`

## 0.18.0 — Print quotas

An optional monthly page limit per person.

- **People & access → Print quotas:** a limit for everyone (members and
  print-only accounts), and, for each person, *Default*, *No limit* or their
  own number. Administrators have no limit; guests keep their own rules (one
  copy, rate-limited).
- **How it counts:** pages printed through Folio this calendar month, in
  your time zone, copies included, after any page range. Prints sent straight
  to the print server from phones don't count, and nor do earlier months.
- **A print that would go over is stopped before it reaches the printer,**
  with the reason: *"This would print 4 pages, but you have 2 of your 5
  pages left this month. Ask an administrator for more."* That works for
  background prints too.
- **People see where they stand** on the Print desk: *"This month you have
  printed 12 of your 20 pages; 8 left."* Administrators see each person's
  pages this month on People & access.
- **Quotas are counted from the print statistics.** If statistics are
  switched off, nobody can be limited; People & access and System check say
  so.

### Verified
- **Tests:** 208 on the host. Inside the images, on amd64: Full 209, Lite
  206 with 3 skipped. They cover:
  - the default and per-person limits, and "no limit"
  - the exact refusal before anything reaches the print server
  - administrators and guests being exempt
  - background prints
  - earlier months and direct prints not counting
  - the warning when statistics are off
  - members being unable to change limits
- **In headless Chrome:**
  - setting a limit for everyone, then one person's own limit
  - that person seeing their usage on the Print desk at phone width (375 px)
- **Start-up check:** both images start healthy, with Folio as the
  ordinary user.
- **Not verified:** the arm64 images (built and tested by the release
  workflow).

### Upgrading
Nothing is required. There are no limits until you set one.

## 0.17.0 — Scans go where you keep documents

Finished documents can be sent straight to Paperless-ngx or a folder, and
blank pages no longer clutter feeder scans.

### Send to Paperless-ngx or a folder
- An administrator adds destinations under **Devices → Destinations**:
  - **Paperless-ngx:** its address and an API token. **Check** proves both
    work and lists Paperless's tags, so every document sent there can be
    tagged.
  - **A folder:** any host folder mounted into the container under
    `/destinations`, such as a NAS share or Paperless's consume folder.
- On the workbench, **Send** (next to Export) builds the PDF in the
  background and delivers it:
  - Paperless gets it with its title and tags, then reads and files it.
  - A folder gets `2026-10-27 Title.pdf`, written so that folder watchers
    never see half a file.
- The document then shows *Sent to … (date)*. Anyone working on documents
  can send their own; print-only accounts can't.
- Paperless's API token never reaches a browser. Paperless problems (a
  renewed token, an unreachable server) are explained in plain words.

### Blank pages
- **Leave out blank pages** (capture pane): when scanning through the
  feeder, empty pages, such as the backs of one-sided sheets, go straight to
  the deleted pages, and **Undo** brings each back to its place. Duplex
  front and back batches keep theirs, because they must stay paired.
- **Remove blank pages** (organize pane) does the same for a document
  already scanned.
- **What counts as blank:**
  - **Blank:** dust, show-through from the other side, grey or cream paper,
    scanner-edge shadows, and a lone page number.
  - **Never blank:** a signature, initials, or a single small line of text.
  - It works in both editions, without OpenCV.

### Verified
- **Tests:** 202 on the host. Inside the images, on amd64: Full 203, Lite
  200 with 3 skipped.
- **Blank detection** on test pages at 300 DPI: empty paper, grey paper,
  300 dust specks, blurred show-through, an edge shadow and a page number
  count as blank; initials, a thin signature, one small line and full text
  don't. Scanning with the option on, removing pages later, the order Undo
  restores them in, and duplex batches are covered by tests.
- **Against a real Paperless-ngx** (a throwaway one, latest image):
  - Folio's Check listed its tags.
  - A document sent from Folio arrived with its title, file name and the
    chosen tag, and Paperless read its text.
- **With a fake Paperless:** a wrong token, an unreachable server, a token
  renewed after setup, and tags spread over several pages are handled.
- **In headless Chrome:**
  - removing a blank page
  - adding a folder destination
  - sending through Send: the PDF appeared in the mounted folder, and the
    document shows "Sent to NAS"
- **Start-up check:** both images start healthy, with Folio as the
  ordinary user.
- **Not verified:** blank detection on real scanner output (only test pages
  so far, so compare with your scans), and the arm64 images (built and
  tested by the release workflow).

### Upgrading
Nothing is required. To send to a folder, mount it under `/destinations`
(see [Deployment](docs/DEPLOYMENT.md#sending-documents-to-paperless-ngx-or-a-folder))
and let the user Folio runs as write to it.

## 0.16.0 — The first minutes

Installing Folio no longer needs a password in a settings file, Folio no
longer runs as root, and everything can be backed up and restored from the
browser.

### Create the administrator in the browser
- `SCAN_STUDIO_ADMIN_PASSWORD` is now optional. Without it, open Folio after
  starting it and create the administrator account on the welcome page.
- For safety that's possible only in the first 15 minutes after Folio
  starts, so a forgotten installation that others can reach can't be
  claimed later. `docker compose restart` reopens the window.
- Two people trying at once get one administrator.
- Setting the password in `.env` still works as before.

### A getting-started checklist
The workbench shows administrators the first steps, each ticked off by what
is actually set up:
- add a printer
- print a test page
- add a scanner
- optional: invite people, let phones print, download a backup

Each step has a button that takes you there; **Hide this** when you're done.

### Folio no longer runs as root
- Folio's web application runs as an ordinary user, `PUID`/`PGID` (1000 by
  default). Its files in the data folder belong to that user on the host.
  Existing installations are handed over automatically at the first start.
- Only the built-in print server's small supervisor stays root. It now also
  writes the AirPrint announcements (the host's folder belongs to root):
  - checked every 30 seconds, so new printers appear within half a minute
  - withdrawn when the container stops, so phones don't look for printers
    that are gone
- `PUID=0` runs Folio as root again, for USB scanners only root may open.

### Backup and restore
- **Devices → Backup → Download a backup:** one file with your documents
  (original scans and edits), accounts, settings, print statistics, logins
  for other print servers, and the built-in printers with their drivers.
  Preview caches and sign-ins are left out.
- **Restore from a backup** puts it all back, on this installation or a new
  one:
  - the built-in printers in it are set up again, with their drivers, even
    when the printer itself is switched off
  - what it replaces is kept in `data/.before-restore`
  - you stay signed in if your account is in the backup
- Backups from a newer Folio, files that aren't Folio backups, and archives
  with anything unexpected inside are refused before anything is touched.

### Also
- System check no longer reports an error when the built-in print server
  has no printers yet. It said "No destinations added", which is just CUPS
  having nothing to announce.
- The CI start-up check also confirms Folio runs as the ordinary user.

### Verified
- **Tests:** 184 on the host (the real-CUPS test runs only inside the
  images). Inside the images, on amd64: Full 185, Lite 182 with 3 skipped
  (the auto-straighten tests).
- **A real installation without a password, as the ordinary user:**
  - the processes run as expected: Folio as `folio`; the supervisor and CUPS
    as root
  - the data belongs to `folio`, except the print server's folder; Folio's
    print-server login is readable by its group only
  - the administrator was created in the browser, and a second attempt was
    refused
  - a printer was added on the built-in server
  - a backup with that printer's driver was made; the printer was removed,
    the backup restored, and the printer came back and was announced again
  - stopping the container withdrew the announcements
- **Upgrade:** a 0.15 installation (all files root's) was started as 0.16:
  - Folio's files were handed to `folio`, the print server's stayed root's
  - the account signed in, and the document and built-in printer were still
    there
- **`PUID=0`:** Folio runs as root and answers.
- **Start-up check:** both images start healthy, with Folio as the ordinary
  user and the built-in print server answering.
- **In headless Chrome:**
  - the welcome page at phone width (375 px): no sideways scrolling
  - creating the administrator, and the checklist
  - the backup panel, and restoring a backup through it; the page reloads
    still signed in
  - Downloading through the page wasn't checked here, because this test
    setup's browser can't save downloads. The same file, fetched directly,
    restores fine.
- **Not verified:** USB scanners as the ordinary user (no USB hardware),
  and the arm64 images (built and tested by the release workflow).

### Upgrading
Nothing is required.
- At the first start, Folio hands its files in the data folder to user and
  group 1000. If your own ids differ (`id -u`, `id -g`), set `PUID` and
  `PGID` first, so the files end up yours on the host.
- `SCAN_STUDIO_ADMIN_PASSWORD` can stay in your settings; it is only used
  when no accounts exist.

## 0.15.0 — Folio is the print server

Folio now includes its own print server, so printing works without setting
up CUPS anywhere else.

### Built-in print server
- **CUPS with the printer drivers Debian ships** for thousands of models
  (Gutenprint, HPLIP, foo2zjs, SpliX, the OpenPrinting collections and
  more), in both editions. Folio sets it up, connects to it automatically
  and restarts it if it stops. **Devices → Add a printer** works right after
  installing: type the printer's address or search, choose *Driverless* or
  your model's driver.
- Its printers, drivers, settings and job history live in `data/cups` and
  survive upgrades.
- It is listed under **Devices → Print servers** as *Built-in print server*.
  It can't be removed there; `FOLIO_BUILTIN_CUPS=off` turns it off. CUPS on
  other computers works exactly as before, under *Add another print server*.
- It accepts administration only from inside the container, with a login
  Folio generates for itself. Other devices can print to it only if you
  publish its port, 631.
- **USB printers** (untested; we have none): pass `/dev/bus/usb` to Folio's
  container, then search for the printer in Add a printer.

### AirPrint for phones and computers
Mount the host's `/etc/avahi/services` into the container and publish port
631, and Folio announces every built-in printer through the host's own
Avahi (Raspberry Pi OS and most Linux desktops run it). Phones and computers
then find the printers by themselves. Announcements follow printers being
added and removed, and are refreshed every 10 minutes. If you publish 631 as
another host port, set `FOLIO_AIRPRINT_PORT`.

Folio deliberately doesn't use the host's D-Bus for this. That would let
CUPS announce on its own, but it would also give the container root-level
control of the host.

### Printer supplies
- Each printer card in Devices shows **toner and ink levels**, with wearing
  parts (imaging unit, fuser, rollers…) underneath, as the printer reports
  them. Serial numbers printers add to part names are left out.
- Print desk shows *Running low: …* for the chosen printer, so everyone sees
  it before printing. System check lists anything low on any printer.

### Statistics: prints sent straight to the print server (optional)
New setting under **Statistics → What Folio keeps**: *Also count prints
sent straight to the print server (from phones and computers)*. It's off
by default, so statistics stay Folio-only unless you choose otherwise. When
it's on, Folio checks each print server it can administer every 10 minutes
and whenever Statistics opens. Counting starts from the moment you switch
it on, and Folio's own jobs are never counted twice. Pages are as CUPS
reports them; some printers report none.

### Privacy: Folio's files are now private to Folio
The print server's document converters run as a separate, unprivileged
user. To keep them, or anything else on the host, away from accounts and
documents, Folio now:
- makes its own files private (accounts, print log, device settings)
- creates every new file private
- lets other users pass through the data folder but not list or read it

Existing installations are fixed at the first start of 0.15.

### Also
- `compose.cups.yaml` is gone. The built-in print server replaces it, and
  CUPS on other computers is still supported.
- System check names the built-in print server, says whether its printers
  are announced on the network, and reports supplies.
- The CI start-up check now also confirms the built-in print server is
  running in each image.
- Images are larger: about 1.3 GB for Full (+81 MB) and 890 MB for Lite
  (+229 MB) on disk.

### Verified
- **Tests:** 172 on the host; the real-CUPS test runs only inside the
  images. Inside the images, on amd64: Full 173, Lite 170 with 3 skipped
  (the auto-straighten tests).
- **A real end-to-end test inside each image**, with a real CUPS:
  - Folio adds a printer to the built-in server through its API, with a
    stand-in JetDirect printer on a local port.
  - A PDF is printed through the normal Print desk path, and CUPS's filters
    deliver PostScript to the stand-in printer.
  - Statistics count it, the AirPrint announcement is written from the real
    queue's attributes, and a job sent straight to CUPS "from a phone" is
    counted once the setting is on.
- **Access:** administration is refused from outside the container even with
  the correct password, and refused inside it without Folio's login.
- **File privacy:** in a running container, the converters' user can't list
  the data folder or read the account database, print log or Folio's
  print-server password; it can reach only CUPS's own folder.
- **Stopping:** the container stops in under a second, CUPS included.
- **Start-up check:** both images start healthy, with the built-in print
  server answering.
- **Supplies:** read from the maintainer's Samsung M2885FW, read-only:
  black toner, imaging unit, fuser, rollers and document-feeder parts, with
  serial numbers removed.
- **In headless Chrome at desktop and phone width (375 px):**
  - the Devices page and the built-in server card
  - adding a printer on the built-in server
  - the supplies panel
  - the Print desk status and the new Statistics setting
  - System check

  No errors and no sideways scrolling.
- **Not verified:**
  - USB printers (none available)
  - an iPhone or Mac actually finding a printer through the announcement
    (its content mirrors what CUPS announces and was generated from a real
    queue)
  - the arm64 images, which are built and tested by the release workflow on
    GitHub's ARM machines

### Upgrading
Nothing is required.
- **The built-in print server appears next to your existing print servers;**
  your printers and default printer stay as they are. If you only want your
  own CUPS server, set `FOLIO_BUILTIN_CUPS=off`.
- **If you used `compose.cups.yaml`,** keep it as it is, or move to the
  built-in server: add your printers again in Folio, then remove the `cups`
  service.
- **Backups:** the print server's files belong to root, so make backups
  with `sudo tar …`.

## 0.14.0 — Ready for GitHub: published images, CI and community docs

Folio is now set up as a public open-source project. Nothing changes in
how Folio works; this release is about installing and contributing.

### Install without building
- Images are published to the GitHub Container Registry, and mirrored to
  Docker Hub, for **x86-64 and 64-bit ARM** (Raspberry Pi 3/4/5 on a 64-bit
  OS): `ghcr.io/kmmukut/folio-scan-studio:0.14-full` and `…:0.14-lite`.
  Also `0.14.0-full` (exact), `full`/`lite` (newest), and `latest` (Full).
- `compose.yaml` now pulls the image. Choose the edition with
  `FOLIO_EDITION` and the version with `FOLIO_VERSION` (default `0.14`,
  which receives 0.14.x fixes). `FOLIO_PORT` sets the port.
  `compose.build.yaml` builds from source instead.
- **compose.cups.yaml** adds a CUPS print server next to Folio for hosts
  that don't have one: `docker compose -f compose.yaml -f compose.cups.yaml up -d`.
- `.env.example` explains every setting.

### Documentation and community
- A new README with the project's mission, vision and principles, and
  screenshots.
- Guides: [Deployment](docs/DEPLOYMENT.md) (Compose, Portainer, Raspberry
  Pi, CUPS, USB, reverse proxies, backups), [Configuration](docs/CONFIGURATION.md)
  and [Hardware](docs/HARDWARE.md), with the start of a compatibility list.
- CONTRIBUTING, SECURITY (private vulnerability reporting), and issue
  forms for bugs, features and **hardware reports**.

### License
Folio is licensed under the **GNU AGPL-3.0-or-later**, matching PyMuPDF and
Ghostscript, which the images include. The sign-in page, the sidebar and
the guest page link to the source code, as the AGPL asks of software used
over a network; the license is also in the image at `/app/LICENSE`.

### Automated builds
- **CI** on every push and pull request: tests, an error-level lint (ruff),
  a syntax check of the page script, both images built, the test suite run
  inside each image, and a start-up check (version, edition, Docker health).
- **Releases** from a version tag: each edition is built natively on amd64
  and arm64, tested on its own architecture, published with an SBOM and
  build provenance, and a GitHub release is created from this changelog.
- **Dependabot** proposes weekly updates for Python packages, the base image
  (now pinned by digest, for reproducible builds) and the workflow actions.

### Also
- The sidebar no longer says "Private edition"; it shows Full or Lite.
- System check explains an untrusted reverse proxy in plain words (what
  goes wrong and the exact setting to add), and counts properly ("1
  document", "1 printer, 1 ready", "1 job on record").
- Personal network addresses in tests and old changelog entries were
  replaced with documentation addresses; a test now rejects any address in
  shipped files that isn't loopback, a documentation range or an obvious
  placeholder.
- Unused imports removed.

### Verified
- Full test suite on the host and inside both images (amd64).
- The CI scripts (in-image tests, start-up check, release notes) were run
  locally against both images; the workflows pass `actionlint` and
  `shellcheck`.
- `compose.yaml` + `compose.cups.yaml` started together from scratch: the
  CUPS container filled its configuration, Folio listed `cups:631`, the
  administrator login connected, 80 driver makes were listed and a printer
  was added.
- **Not verified yet:** the workflows themselves and the arm64 images. They
  run for the first time on GitHub; the arm64 build and its tests run on
  GitHub's ARM machines.

### Upgrading
Data is unchanged. To move from a locally built image to the published one,
replace your `compose.yaml` with the new one (keep your `.env`), add
`FOLIO_EDITION` and `FOLIO_VERSION=0.14` if you like, then
`docker compose pull && docker compose up -d`. In Portainer, update the
stack's compose text the same way and keep the data path you use today.

## 0.13.0 — Print statistics

Administrators get a **Statistics** page: how much was printed, by whom, on
which printer and when, from a log Folio keeps of every job it sends.

- **Totals for a period** (last 30 days, this or last month, this year, last
  12 months, all time): pages printed, sheets of paper, print jobs, and
  sheets saved by double-sided and multi-page printing, plus how many pages
  were in colour, in greyscale, or left to the printer ("Automatic" on a
  colour printer).
- **Sheets of paper per day or month**, as a chart with a table view.
- **By person and by printer.** Guests are counted together as "Guests".
- **Download CSV** of the individual jobs in the chosen period.
- **Record keeping:** keep statistics for 3 months, 1 year (default), 2 or 5
  years, or until deleted; switch them off; or delete them all.

What is recorded, per job: time, Folio user, print server and printer,
whether it came from a scanned document, an upload or a test page, pages,
copies, pages per sheet, one- or two-sided, sheets, colour mode, paper size,
and the CUPS job number. **Document names and contents are never stored.**
Deleting an account removes its name from the records (its pages still
count for the printer). Only administrators can see or change any of this.

How the numbers are worked out: pages are the pages Folio sent (after any
page range) times copies; sheets also account for pages per sheet and
double-sided printing, with each copy starting on a new sheet. A job
cancelled in Folio is marked and left out of the totals. Folio does not know
what the printer physically did, so a paper jam or a job cancelled at the
printer still counts. Jobs sent to CUPS from other computers are not
included; Folio only counts what it sent itself.

The log is a separate SQLite file, `data/print-log.sqlite3`, apart from
accounts, so it can be switched off or deleted without touching anything
else. If it ever cannot be written, printing carries on; System check shows
its state. No new service or container is needed.

### Also
- New setting `TZ` (default `UTC`) so jobs count on the right local day and
  month, e.g. `TZ=Europe/Berlin`.

### Verified
- 16 new tests: page and sheet arithmetic (copies, duplex, pages per sheet),
  periods and day/month grouping including time zones, retention, switching
  off, cancelled jobs, removed accounts, every way of printing (upload,
  background job, saved document, test page, guest), statistics never
  stopping a print, administrator-only access for every endpoint, and CSV
  cells that cannot run as spreadsheet formulas.
- The whole test suite runs inside both built images: Full 158 passed;
  Lite 155 passed and 3 skipped (the auto-straighten tests).
- End to end against a real CUPS (a throwaway container, printers pointing
  at unused addresses so nothing printed): uploads, a 2-up job and a test
  page went through `lp`, the CUPS job numbers were recorded, a job
  cancelled in Folio dropped out of the totals, and times followed `TZ`.
- In headless Chrome at desktop and phone width (375 px): the page, chart
  hover, period switching, CSV download, retention change (asks before
  deleting older records), switching off, deleting everything, and that a
  member does not see the page. No horizontal scrolling at either width.
- **Not verified:** with your printers. Counts are what Folio sent, so they
  do not depend on the printer, but check a few against real prints.

### Upgrading
Nothing to change; the print log starts empty and fills from the first job
after the upgrade (earlier prints are not counted). Optionally set `TZ` in
`.env` or the Portainer stack variables. The image tag is now
`folio-scan-studio:0.13.0-full` (or `-lite`).

## 0.12.0 — Lite and Full editions from one codebase

Folio can now be built in two editions from the same source, chosen with a
single build setting (`FOLIO_EDITION=full` or `lite`):

| | Full (default) | Lite |
|---|---|---|
| Image size (x86-64, uncompressed) | 1.2 GB | **659 MB** |
| Scanning, editing, PDF export, printing, printer management, accounts | ✓ | ✓ |
| Searchable PDF (OCR) | ✓ | — |
| Automatic straightening suggestion (manual straightening works in both) | ✓ | — |

Lite leaves out OCRmyPDF, Tesseract and Ghostscript (which also pulled in a
second Python, Perl and several video codecs) and OpenCV/NumPy. The page
hides what an installation lacks, and the API answers a clear
"not included in the Lite edition" (HTTP 409) instead of failing. Features
are detected from what is installed, not from the edition name.

### Print servers in Devices, and a clearer "Add a printer"
- **Devices → Print servers** lists every CUPS server with its status and
  administrator login, and **Add a print server** adds one by address (Folio
  checks it answers first; port 631 is assumed). Servers from `CUPS_SERVERS`
  still work and are marked "From the CUPS_SERVERS setting"; only servers
  added in Folio can be removed there. Removing one makes Folio stop using
  it and forget its login and printer settings; nothing changes on the server.
  `CUPS_SERVERS` is now optional.
- **Add a printer** is four numbered steps: 1 Print server (the computer
  running CUPS), 2 The printer (type *the printer's* IP address, or search),
  3 Driver, 4 Name and access. A missing printer address is explained
  instead of the button appearing to do nothing.
- The Devices page now reads Printers → Add a printer → Print servers, so
  server logins no longer look like printers.

### Fixed: new versions could load with old JavaScript
After an upgrade, browsers (and Nginx Proxy Manager with "Cache Assets"
switched on, which keeps scripts for 30 minutes whatever the server says)
could keep running the previous `app.js` with the new page, so new buttons
did nothing until a hard refresh. Folio never told browsers how long to keep
its files. The page now loads `app.js` and `style.css` under a fingerprint
of their contents (`/assets/app.js?v=…`) and is itself always revalidated,
so a changed file always has a new address. No proxy setting needs changing.

### Also
- OpenCV now loads only when auto-straighten is actually used, so the Full
  edition's idle memory dropped too: 76 MB in 0.11 → 60 MB (Full) and
  59 MB (Lite), measured right after startup. Under load (previews and a
  PDF export of six 300 DPI pages) all editions peak around 365–385 MB,
  because decoding scans dominates; Lite saves disk and download, not
  working memory.
- System check shows the edition; `/api/features` reports it.
- The image keeps Debian slim as its base. Alpine was evaluated and
  rejected: PyMuPDF publishes no Alpine wheel for ARM64 (a Raspberry Pi
  would compile MuPDF during every build) and OpenCV none at all.

### Verified
- The whole test suite runs inside both built images: Full 142 passed;
  Lite 139 passed and 3 skipped (the auto-straighten tests), repeated
  runs each, with NumPy, OpenCV and OCRmyPDF genuinely absent
  in Lite. A test also imports Folio with those modules blocked.
- In headless Chrome: Lite shows "Lite edition" and hides Searchable PDF and
  Auto straighten; Full shows both.
- **Not verified:** building or running on ARM64 / a Raspberry Pi. The
  wheels Folio needs exist for ARM64 (glibc), but no image was built there.

### Upgrading
Existing setups stay on Full and need no change; the image tag is now
`folio-scan-studio:0.12.0-full` (or `-lite`). To switch an installation to
Lite, set `FOLIO_EDITION=lite` in `.env` or the Portainer stack variables and
rebuild. Data is shared between editions; documents exported as searchable
PDFs before the switch stay searchable.

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

