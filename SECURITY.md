# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them
privately through GitHub: on the repository's **Security** tab choose
**Report a vulnerability**. Only the maintainers can see the report.

Include what you found, how to reproduce it, the Folio version (shown at
the bottom of the sidebar) and the edition. Folio is maintained by
volunteers; we aim to reply within a week, and will tell you when a fix is
released and credit you if you wish.

## Supported versions

Fixes go into the newest release. Before 1.0, only the latest minor
version (currently 0.21.x) receives security fixes, so please keep up to
date: `docker compose pull && docker compose up -d`.

## How Folio protects your data

- Everything runs on your own network. Folio makes no connections to the
  internet and sends no telemetry.
- Folio's web application runs as an ordinary user (`PUID`), not root.
  Only the built-in print server's small supervisor runs as root.
- A new installation's administrator can be created in the browser only
  in the first 15 minutes after Folio starts, so a forgotten, exposed
  installation can't be claimed later. Backups hold accounts and logins:
  store them safely.
- Passwords are stored as salted PBKDF2 hashes. Sessions use HTTP-only,
  same-site cookies with CSRF tokens; set `COOKIE_SECURE=true` behind HTTPS.
- Members see only their own documents and print jobs; every request is
  checked on the server.
- CUPS administrator logins are stored readable by root only, never sent to
  browsers, and an encrypted connection's certificate is pinned before any
  password is sent.
- The built-in print server accepts administration only from inside the
  container, with a login Folio generates for itself. Its document
  converters run as an unprivileged user that cannot read Folio's accounts
  or documents. Printing to it from the network is possible only if you
  publish its port (631).
- Printers are announced on the network through files in the host's Avahi
  folder; Folio is never given the host's D-Bus, which would grant it far
  more control over the host.
- Paperless-ngx API tokens are stored only on the Folio server and never
  sent to browsers. Certificate checking can be switched off per
  destination for self-signed certificates; it is on by default.
- Print options are passed to CUPS from an allowlist; uploads are validated
  and size-limited; guest printing is off by default and rate-limited.

## Deployment advice

Folio is designed for a home or office network. If you make it reachable
from the internet, put it behind an HTTPS reverse proxy (see
[Deployment](docs/DEPLOYMENT.md#behind-a-reverse-proxy)), use strong
passwords, and keep guest printing off unless you need it.
