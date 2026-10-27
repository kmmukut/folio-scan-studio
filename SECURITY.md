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
version (currently 0.14.x) receives security fixes, so please keep up to
date: `docker compose pull && docker compose up -d`.

## How Folio protects your data

- Everything runs on your own network. Folio makes no connections to the
  internet and sends no telemetry.
- Passwords are stored as salted PBKDF2 hashes. Sessions use HTTP-only,
  same-site cookies with CSRF tokens; set `COOKIE_SECURE=true` behind HTTPS.
- Members see only their own documents and print jobs; every request is
  checked on the server.
- CUPS administrator logins are stored readable by root only, never sent to
  browsers, and an encrypted connection's certificate is pinned before any
  password is sent.
- Print options are passed to CUPS from an allowlist; uploads are validated
  and size-limited; guest printing is off by default and rate-limited.

## Deployment advice

Folio is designed for a home or office network. If you make it reachable
from the internet, put it behind an HTTPS reverse proxy (see
[Deployment](docs/DEPLOYMENT.md#behind-a-reverse-proxy)), use strong
passwords, and keep guest printing off unless you need it.
