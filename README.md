# PostgreSQL Migration Rehearsal

Rehearse a small invoice-schema change in disposable local PostgreSQL and inspect the migration, integrity checks and cleanup separately.

## Current reproducibility limit

The browser interface starts, but the real rehearsal currently rejects a fresh build of its PostgreSQL image. The integrity check expects a previously frozen local image identity that is not available in this checkout. A successful build is not enough to satisfy that check. The failure occurs before database work. Do not weaken the hash check to force a pass. The current supported recruiter format is the static walkthrough.

## Inspect the synthetic example locally

Requires Python 3.12.14 and uv. PMR also requires Node 24.15.0, npm 11.18.0 and a local Docker Engine with Compose; its PostgreSQL image is built from the pinned Dockerfile.

From this checkout:

```sh
uv sync --frozen
npm ci --ignore-scripts
npm run control-room:start
```

Open the loopback URL printed by the launcher. The six-invoice synthetic scenario is visible. Running it requires the original verified runtime image; without it, expect “Local rehearsal unavailable”. Inspect the five stages, resulting fields and cleanup status. The negative-amount sample is rejected before database work. A migration verdict and cleanup confirmation are separate observations.

Stop the server when finished:

```sh
npm run control-room:stop
```

## What this public release contains

This is a clean public source snapshot of the local project, not a copy of its private Git history. Application source files and synthetic input files are unchanged from the verified local implementation. Private orchestration records, author paths, archived build-proof machinery and one-shot research runners are not distributed. Their canonical local versions and history remain preserved.

The tests shipped here cover the public runtime. They are a defined subset of the larger local verification suite, not a claim that every historical control is reproduced by this package. `SOURCE_MANIFEST.json` identifies every copied file, and `PUBLIC_RELEASE_SCOPE.json` lists the selected runtime tests and the omitted verification categories.

```sh
uv run --frozen pytest -q
```

## Limits

This is a small local rehearsal, not a production migration certificate. Historical Phase B is display-only and has no runnable endpoint. Browser-dependency provenance remains incomplete (LOW02); installed hashes do not attest every upstream tarball or supporting library.

The browser server is designed for a local machine. Do not expose it directly on the Internet. The recruiter preview is a separate static explanation with recorded synthetic results.

## My role

I use AI extensively to build these projects. I understand and review the code, and I am still learning to write it independently.
