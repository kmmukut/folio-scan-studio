#!/bin/sh
# Folio's container start, as root:
#  1. the built-in print server (CUPS), kept running by Folio's supervisor,
#     unless FOLIO_BUILTIN_CUPS=off;
#  2. Folio itself as an ordinary user, PUID/PGID (default 1000), so the data
#     folder belongs to that user on the host too. PUID=0 keeps Folio as root,
#     for USB scanners whose device files only root may open.
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DATA_DIR="${DATA_DIR:-/data}"
case "$PUID$PGID" in *[!0-9]*) echo "PUID and PGID must be numbers" >&2; exit 1 ;; esac

if [ "$PUID" != 0 ]; then
  getent group folio >/dev/null || groupadd -o -g "$PGID" folio
  [ "$(getent group folio | cut -d: -f3)" = "$PGID" ] || groupmod -o -g "$PGID" folio
  id folio >/dev/null 2>&1 || useradd -o -u "$PUID" -g folio -M -d /tmp -s /usr/sbin/nologin folio
  [ "$(id -u folio)" = "$PUID" ] || usermod -o -u "$PUID" folio
  mkdir -p "$DATA_DIR"
  # Hand Folio its files: everything except the print server's own folder.
  # Later starts find nothing left to change, so this is quick.
  find "$DATA_DIR" -xdev -path "$DATA_DIR/cups" -prune -o ! -user "$PUID" -exec chown -h "$PUID:$PGID" {} +
fi
export FOLIO_GID="$PGID"  # The print server lets this group read Folio's login to it

case "$(echo "${FOLIO_BUILTIN_CUPS:-on}" | tr '[:upper:]' '[:lower:]')" in
  off|false|no|0) rm -f /etc/avahi/services/folio-*.service 2>/dev/null || true ;;
  *) python -m backend.printserver & ;;
esac

# Everything Folio creates is private to it (the print server's helpers run
# as yet another user and must not read accounts or documents).
umask 077
if [ "$PUID" = 0 ]; then
  exec "$@"
fi
export HOME=/tmp
exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups -- "$@"
