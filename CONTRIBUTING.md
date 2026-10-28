# Contributing to Folio

Thank you for helping old printers and scanners stay useful. Every kind of
contribution counts, and you don't need to write code to make a difference.

## Ways to help

- **Hardware reports.** Tell us how Folio worked with your printer or
  scanner, including when it didn't. Use the
  [hardware report](https://github.com/kmmukut/folio-scan-studio/issues/new?template=hardware_report.yml)
  form; reports become the [compatibility list](docs/HARDWARE.md).
- **USB devices and Raspberry Pi.** The maintainers can't test these.
  Working passthrough recipes, notes on specific models, and Pi experience
  are especially welcome.
- **Bugs.** Open a
  [bug report](https://github.com/kmmukut/folio-scan-studio/issues/new?template=bug_report.yml)
  with the output of **Devices → Run checks**.
- **Ideas.** Open a feature request describing the problem you want solved.
  Folio deliberately stays small, so please explain who it helps.
- **Code and documentation.** For anything larger than a small fix, open an
  issue first so we can agree on the approach before you spend time on it.

## How we work

These are the rules Folio has been built by:

- **Inspect first, change incrementally.** Read the code around what you
  change; prefer small, focused changes over rewrites.
- **Add a regression test** for every fix and feature.
- **Never hardcode hardware.** Paper sizes, trays, duplex, colour, scan
  modes and resolutions come from what CUPS or SANE report. No device names,
  addresses or networks in the code (a test enforces this).
- **Stay lightweight.** No frontend build step or framework, no external
  database or service. Heavy dependencies belong in the Full edition only,
  imported lazily (see [Architecture](docs/ARCHITECTURE.md), D4 and D10).
- **Keep originals sacred.** Edits are non-destructive; scans on disk are
  never changed.
- **Be honest about verification.** In your pull request, say what you
  tested and how, and what still needs testing on real hardware. "Untested
  on USB" is a perfectly good answer; a claim that wasn't checked is not.
- **Match the surrounding code:** its naming, comment density and idiom.

## Development setup

You need Python 3.12 and Docker.

```bash
git clone https://github.com/kmmukut/folio-scan-studio.git && cd folio-scan-studio
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q          # the whole suite; no hardware needed
ruff check backend tests     # errors only (unused imports, undefined names)
node --check frontend/app.js && node --check frontend/sw.js   # or in node:22-alpine with Docker
```

Tests simulate scanners and CUPS (including an in-process CUPS that speaks
real IPP), so they never need devices and never write outside temporary
folders.

To run Folio from your checkout:

```bash
cp .env.example .env               # optional: SCAN_STUDIO_ADMIN_PASSWORD, or create the admin in the browser
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

**Inside the images.** The Lite edition must not import NumPy or OpenCV at
startup, and some tests (a real CUPS, the scanner tools) only run where the
image's software really is. Run the suite inside each image, then start it
the way users do:

```bash
docker build --build-arg FOLIO_EDITION=lite -t folio-scan-studio:lite .
.github/scripts/test-image.sh folio-scan-studio:lite        # the suite, inside the image
.github/scripts/smoke-test.sh folio-scan-studio:lite lite   # starts it and checks it works
```

Tests that need OpenCV are marked `full_only` and skip themselves in Lite.

## Pull requests

- Branch from `main`; one topic per pull request.
- CI must pass: tests on the host, both images built, the suite run inside
  each image, and a start-up check of each image.
- Add a line to the `## Unreleased` section of [CHANGELOG.md](CHANGELOG.md)
  for anything users would notice.
- Fill in the pull request template, especially *How was this tested?*

By contributing, you agree that your contribution is licensed under the
[AGPL-3.0-or-later](LICENSE), like the rest of Folio.

## Releases (maintainers)

1. Set the version in `backend/version.py`, `Dockerfile` (`FOLIO_VERSION`),
   `frontend/index.html`, `compose.yaml` and `.env.example`
   (`FOLIO_VERSION`, major.minor). A test checks they agree.
2. Rename `## Unreleased` in the changelog to `## X.Y.Z — Title`, with
   verification notes and upgrade steps.
3. Merge to `main`, then tag and push: `git tag -a vX.Y.Z -m "Folio vX.Y.Z" && git push origin vX.Y.Z`.

The release workflow then tests both editions on amd64 and arm64, publishes
the images to GHCR and Docker Hub, and creates the GitHub release with that
version's changelog and a source archive. A tag like `v1.0.0-rc.1` is
published as a pre-release and doesn't move `latest`, `full` or `lite`.
