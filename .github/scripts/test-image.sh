#!/bin/sh
# Run the test suite inside a built image, where the edition's optional
# packages are really present (Full) or really missing (Lite).
# Usage: .github/scripts/test-image.sh IMAGE   (from the repository root)
set -eu
image="$1"
tools=$(grep -E '^(pytest|httpx|pypdf)==' requirements-dev.txt | tr '\n' ' ')
docker run --rm --entrypoint sh -v "$PWD:/src:ro" "$image" -c "
  set -e
  python -m ensurepip --default-pip >/dev/null 2>&1   # The image ships without pip
  python -m pip install --quiet --disable-pip-version-check --root-user-action=ignore $tools
  mkdir /tmp/w && tar -C /src --exclude=./.git --exclude=./.venv --exclude=./data -cf - . | tar -C /tmp/w -xf -
  cd /tmp/w && python -m pytest -q -p no:cacheprovider"
