#!/bin/sh
# Start an image the way users do and check that it answers, reports the
# expected version and edition, and that Docker's health check passes.
# Usage: .github/scripts/smoke-test.sh IMAGE EDITION   (from the repository root)
set -eu
image="$1" edition="$2"
version=$(sed -n "s/^__version__ = '\(.*\)'$/\1/p" backend/version.py)
name="folio-smoke-$$"
docker run -d --name "$name" -p 127.0.0.1::8870 \
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
i=0
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$name")" = healthy ]; do
  i=$((i + 1)); [ $i -lt 45 ] || { echo "Docker health check did not pass within 90 s"; exit 1; }
  sleep 2
done
echo "Docker reports the container healthy"
