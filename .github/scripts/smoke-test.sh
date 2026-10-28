#!/bin/sh
# Start an image the way users do and check that it answers, reports the
# expected version and edition, and that Docker's health check passes.
# Usage: .github/scripts/smoke-test.sh IMAGE EDITION   (from the repository root)
set -eu
image="$1" edition="$2"
version=$(sed -n "s/^__version__ = '\(.*\)'$/\1/p" backend/version.py)
name="folio-smoke-$$"
docker run -d --name "$name" -p 127.0.0.1::8870 -p 127.0.0.1::631 \
  -e SCAN_STUDIO_ADMIN_PASSWORD="smoke-test-password-$$" "$image" >/dev/null
cleanup() { status=$?; [ $status -eq 0 ] || docker logs "$name" 2>&1 | tail -40; docker rm -f "$name" >/dev/null; exit $status; }
trap cleanup EXIT
port=$(docker port "$name" 8870/tcp | head -n 1 | sed 's/.*://')
url="http://127.0.0.1:$port"
i=0
until health=$(curl -fsS "$url/api/health" 2>/dev/null); do
  i=$((i + 1)); [ $i -lt 60 ] || { echo "Folio did not answer within 60 s"; exit 1; }
  sleep 1
done
echo "health: $health"
echo "$health" | grep -q "\"version\":\"$version\"" || { echo "expected version $version"; exit 1; }
features=$(curl -fsS "$url/api/features")
echo "features: $features"
echo "$features" | grep -q "\"edition\":\"$edition\"" || { echo "expected edition $edition"; exit 1; }
curl -fsS "$url/" | grep -q 'Source code' || { echo "page lacks the source-code link"; exit 1; }
docker exec "$name" test -f /app/LICENSE || { echo "LICENSE missing from the image"; exit 1; }
# pip is only for building the image; it doesn't ship in it.
if docker exec "$name" python -c 'import pip' 2>/dev/null; then echo "pip is still in the image"; exit 1; fi
# Folio itself runs as an ordinary user; only the print server's supervisor is root.
user=$(docker exec "$name" sh -c 'ps -eo user=,args= | grep "bin/[u]vicorn" | head -n 1 | cut -d" " -f1')
[ "$user" = folio ] || { echo "Folio runs as '$user', not as the folio user"; exit 1; }
echo "Folio runs as: $user"
# Scanning: listing scanners as Folio's user must work (no SANE plug-in may crash it).
docker exec -u folio "$name" timeout 60 scanimage -L >/dev/null 2>&1 || { echo "scanimage -L failed in the image"; exit 1; }
echo "scanner listing: works"
# AirSane (scanners shared as AirScan) is built in, with every library it needs.
docker exec "$name" sh -c '! ldd /usr/local/bin/airsaned | grep -q "not found"' || { echo "AirSane is missing or can't load"; exit 1; }
echo "AirSane: present"
# The built-in print server: signed in, System check must find it answering.
jar=$(mktemp)
csrf=$(curl -fsS -c "$jar" -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"smoke-test-password-$$\"}" "$url/api/auth/login" |
  sed -n 's/.*"csrf":"\([^"]*\)".*/\1/p')
i=0
# A new installation has no printers yet, which System check reports as "warn".
until curl -fsS -b "$jar" "$url/api/admin/diagnostics" |
    grep -Eq '"id":"cups:localhost:631","label":"Built-in print server","status":"(ok|warn)","detail":"Reachable'; do
  i=$((i + 1)); [ $i -lt 30 ] || { echo "the built-in print server did not start"; rm -f "$jar"; exit 1; }
  sleep 2
done
echo "built-in print server: running"
# Folio may not swap the print server's folder for one of its own.
[ "$(docker exec "$name" stat -c %U:%a /data)" = root:1771 ] || { echo "/data is not protected"; exit 1; }
if docker exec -u folio "$name" mv /data/cups /data/cups-moved 2>/dev/null; then echo "Folio could move cups/"; exit 1; fi
docker exec -u folio "$name" sh -c 'touch /data/.smoke && rm /data/.smoke' || { echo "Folio cannot write /data"; exit 1; }
echo "print server's folder: protected"
# CUPS administration, turned on from Folio: HTTPS only, from outside the container.
curl -fsS -b "$jar" -X PUT -H 'Content-Type: application/json' -H "x-csrf-token: $csrf" \
  -d "{\"username\":\"smokeadmin\",\"password\":\"smoke-cups-password-$$\"}" \
  "$url/api/admin/cups/builtin/web-admin" | grep -q '"enabled":true' || { echo "could not turn on CUPS administration"; exit 1; }
rm -f "$jar"
cups="127.0.0.1:$(docker port "$name" 631/tcp | head -n 1 | sed 's/.*://')"
sleep 2  # cupsd reloads its configuration
plain=$(curl -s -o /dev/null -w '%{http_code}' -u "smokeadmin:smoke-cups-password-$$" "http://$cups/admin")
secure=$(curl -sk -o /dev/null -w '%{http_code}' -u "smokeadmin:smoke-cups-password-$$" "https://$cups/admin")
[ "$plain:$secure" = 426:200 ] || { echo "CUPS administration answered $plain over HTTP and $secure over HTTPS"; exit 1; }
echo "CUPS administration: HTTPS only"
i=0
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$name")" = healthy ]; do
  i=$((i + 1)); [ $i -lt 45 ] || { echo "Docker health check did not pass within 90 s"; exit 1; }
  sleep 2
done
echo "Docker reports the container healthy"
