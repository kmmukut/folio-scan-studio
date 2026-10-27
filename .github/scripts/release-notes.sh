#!/bin/sh
# Print one version's section of CHANGELOG.md (without its heading), for the
# GitHub release. Fails if the changelog has no section for that version.
# Usage: .github/scripts/release-notes.sh 0.14.0
set -eu
notes=$(awk -v v="$1" '
  /^## / { if (found) exit; if ($0 == "## " v || index($0, "## " v " ") == 1) { found = 1; next } }
  found { print }' CHANGELOG.md)
[ -n "$(echo "$notes" | tr -d '[:space:]')" ] || { echo "CHANGELOG.md has no section for $1" >&2; exit 1; }
printf '%s\n' "$notes"
