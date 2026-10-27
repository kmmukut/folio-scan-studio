#!/bin/sh
# Starts the built-in print server (CUPS, kept running by Folio's supervisor)
# unless FOLIO_BUILTIN_CUPS=off, then Folio itself.
set -e
case "$(echo "${FOLIO_BUILTIN_CUPS:-on}" | tr '[:upper:]' '[:lower:]')" in
  off|false|no|0) ;;
  *) python -m backend.printserver & ;;
esac
# Everything Folio creates is private to it (the print server's helpers run
# as another user and must not read accounts or documents).
umask 077
exec "$@"
