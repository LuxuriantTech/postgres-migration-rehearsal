# PostgreSQL Migration Rehearsal

[Try the interactive synthetic demo](https://ardian-mehaj-portfolio.vercel.app/projects/postgres-migration-rehearsal/) · [Demo source and local preview](docs/interactive-demo/README.md) · [Contact me](mailto:mehajardian@gmail.com)

Rehearse a small invoice-schema change in disposable local PostgreSQL and inspect the migration, integrity checks and cleanup separately.

## Run the six-invoice CLI baseline

The [PMR-DEMO-20260908-v1 procedure](runtime-baselines/demo-20260908/README.md) uses the public fixture, migration engine and a separately pinned PostgreSQL image. It is a new local rehearsal, distinct from the historical Phase B and browser image. Requires Linux amd64, Python 3.12.14, uv and a local Docker Engine with BuildKit OCI export support. From a clean checkout, use the full build procedure linked above; the essential sequence is:

```sh
uv sync --frozen
docker buildx build --platform linux/amd64 --no-cache --provenance=false \
  --build-arg SOURCE_DATE_EPOCH=1788825600 \
  --output type=oci,rewrite-timestamp=true,dest=/tmp/pmr-demo-v1.oci.tar \
  --metadata-file /tmp/pmr-demo-v1.build.json runtime-baselines/demo-20260908
docker load -i /tmp/pmr-demo-v1.oci.tar
PYTHONPATH=src .venv/bin/python scripts/rehearse_baseline_v1.py
```

Use unused output paths for a later build. The CLI checks `baseline.json`, its source hashes and the image identity before creating a disposable internal network and database. Its JSON result reports the six synthetic rows, migration checks and owned-resource cleanup separately. A different image or Docker executable fails the integrity check; do not change the pin just to make a local run pass.

## Historical browser and static walkthrough

The browser interface starts, but a fresh build of its original PostgreSQL image is rejected before database work: the frozen historical image identity is unavailable in this checkout. The hosted recruiter walkthrough is static and does not start PostgreSQL. Historical Phase B is display-only. [Browser provenance details](docs/BROWSER_PROVENANCE_20260908.md) explain why LOW02 remains open; the separate CLI baseline does not close it.

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

The tests shipped here cover the public runtime. They are a defined subset of the larger local verification suite, not a claim that every historical control is reproduced by this package. `SOURCE_MANIFEST.json` hashes the original release-scope files; the newer CLI's source pins are in `runtime-baselines/demo-20260908/baseline.json`. `PUBLIC_RELEASE_SCOPE.json` lists the original three selected adapter tests and omitted verification categories. The separately added `tests/test_demo_baseline_v1.py` checks the new CLI's pin and refusal behavior; `pytest` runs all four distributed test files, but it does not perform a live Docker migration.

```sh
uv run --frozen pytest -q
```

The prepared [CI workflow](.github/workflows/ci.yml) runs the selected public Python tests and static checks without Docker. Its presence here is not a hosted CI result or a six-invoice runtime test.

Local check on 2026-09-23 (this public snapshot): `uv run --frozen pytest -q` returned 79 passed. The separate no-cache OCI build produced the `baseline.json` manifest digest, and `PYTHONPATH=src .venv/bin/python scripts/rehearse_baseline_v1.py` returned code 0 with six rows, a completed contract phase and `owned_resources_remaining: 0`. This bounded CLI run does not replay historical Phase B or repair the browser baseline.

## Limits

This is a small local rehearsal, not a production migration certificate. Historical Phase B is display-only and has no runnable endpoint. Browser-dependency provenance remains incomplete (LOW02); installed hashes do not attest every upstream tarball or supporting library.

The browser server is designed for a local machine. Do not expose it directly on the Internet. The recruiter preview is a separate static explanation with recorded synthetic results.

## My role

I use AI extensively to build these projects. I understand and review the code, and I am still learning to write it independently.
