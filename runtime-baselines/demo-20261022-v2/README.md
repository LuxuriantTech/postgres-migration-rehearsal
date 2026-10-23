# PMR-DEMO-20261022-v2

This is a new local baseline for the six-invoice CLI rehearsal. It keeps the 2026-09-08 V1 manifest, source hashes and image digest unchanged, and it does not replay or repair historical Phase B or the browser image. It records a current, authenticated Docker toolchain and a fresh build that reproduced the V1 image identity.

## Qualified environment

The recorded run used WSL 2, Ubuntu 24.04.5 LTS, Linux amd64, Python 3.12.14 and uv 0.11.7. Docker CLI and Engine were 29.8.2; Buildx was 0.37.1 with BuildKit 0.33.1. The required Docker context is `default` at `unix:///var/run/docker.sock`.

The exact Docker CLI, Engine and Buildx package versions, signed repository metadata, archive digests and installed executable digests are in [provenance.json](provenance.json). The retained [InRelease](docker-repository/InRelease), [Docker signing key](docker-repository/docker.asc) and [amd64 Packages index](docker-repository/Packages) let the signature-to-binary chain be checked independently. The verifier validates the signing fingerprints and signature, confirms the signed index digest and size, checks each exact version and archive digest against the index, downloads only those bounded exact packages when local `.deb` files are not supplied, extracts them in a temporary directory, and checks the three executable digests. Its recorded output is [package-verification.json](package-verification.json). `dpkg -V` also passed for all three installed packages. The V2 preflight repeats the installed package version and executable hash checks before creating resources.

## Build and run

Use WSL 2 with Ubuntu 24.04.5 LTS on Linux amd64. Keep a fresh checkout in the WSL filesystem so pinned source bytes and Linux bind mounts are stable. The public procedure was checked at repository commit [`626072c`](https://github.com/LuxuriantTech/postgres-migration-rehearsal/tree/626072c0eac87f0b05c287c51f9cc5f150299c9e). Do not run these commands in PowerShell or against a Windows-mounted checkout.

Clone that reviewed snapshot, install the locked Python environment, and confirm the interpreter. If `uv python find` succeeds, no Python download is needed; if it fails, `uv python install` installs the pinned interpreter before dependency sync:

```sh
set -euo pipefail
mkdir -p ~/src
cd ~/src
git -c core.autocrlf=false clone https://github.com/LuxuriantTech/postgres-migration-rehearsal.git pmr-v2
cd pmr-v2
git checkout --detach 626072c0eac87f0b05c287c51f9cc5f150299c9e
uv python find 3.12.14 >/dev/null 2>&1 || uv python install 3.12.14
uv sync --frozen
uv run --frozen python --version
```

The Docker runtime is a separately installed prerequisite. If the official Docker Ubuntu apt source is already configured and these exact versions are available, install or align the three pinned packages with:

```sh
sudo apt-get update
sudo apt-get install --no-install-recommends \
  'docker-ce-cli=5:29.8.2-1~ubuntu.24.04~noble' \
  'docker-ce=5:29.8.2-1~ubuntu.24.04~noble' \
  'docker-buildx-plugin=0.37.1-1~ubuntu.24.04~noble'
```

This may change system packages, so run it only when you intend to prepare the host. If the exact package versions are unavailable, stop rather than substituting the latest version. The signed provenance below authenticates the expected archives and executable hashes; the V2 preflight enforces the installed package versions and hashes.

Install the locked Python environment, then build a temporary OCI archive. The verifier checks the manifest, config, platform and all three recorded BuildKit materials before the archive is loaded:

```sh
uv run --frozen python --version
dpkg-query -W docker-ce-cli docker-ce docker-buildx-plugin
docker context show
docker context inspect default --format '{{.Endpoints.docker.Host}}'
sha256sum /usr/bin/docker /usr/bin/dockerd /usr/libexec/docker/cli-plugins/docker-buildx
docker buildx version
docker buildx inspect --bootstrap
PYTHONPATH=src:scripts uv run --frozen python scripts/verify_docker_repository_provenance.py

pmr_tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/pmr-demo-v2.XXXXXX")"
trap 'rm -rf "$pmr_tmpdir"' EXIT
docker buildx build --platform linux/amd64 --no-cache --provenance=false \
  --build-arg SOURCE_DATE_EPOCH=1788825600 \
  --output "type=oci,rewrite-timestamp=true,dest=$pmr_tmpdir/image.oci.tar" \
  --metadata-file "$pmr_tmpdir/build.json" runtime-baselines/demo-20260908
PYTHONPATH=src:scripts uv run --frozen python scripts/verify_demo_baseline_v2_build.py \
  "$pmr_tmpdir/build.json"
docker load -i "$pmr_tmpdir/image.oci.tar"
PYTHONPATH=src:scripts uv run --frozen python scripts/rehearse_baseline_v2.py --preflight-only
PYTHONPATH=src:scripts uv run --frozen python scripts/rehearse_baseline_v2.py --scenario small-shop
```

The build verifier stops if the fresh output differs from the reviewed manifest, config or materials. The preflight then checks the source digests, Linux amd64 host, package versions, executable hashes, local Docker endpoint and loaded image without creating resources. The rehearsal creates one random-label internal network and one capped disposable database under the `pmr-demo-v2-` prefix; it removes only the immutable IDs it created and reports cleanup separately.

## Recorded result

[image-build.json](image-build.json) and [image-build.log](image-build.log) record the fresh OCI build. Its manifest and config matched the existing V1 identities exactly. The temporary 124,920,320-byte OCI archive was discarded after verification; it is not included in the repository.

[rehearsal-result.json](rehearsal-result.json) is the exact V2 runner output. Six source rows matched six destination rows in EUR minor units. The migration reached the contract phase, both backfill passes were idempotent on their second pass, rollback restored the legacy rows, roll-forward completed, and cleanup reported `complete` with zero owned resources remaining. The post-run `pmr-demo-v2-` container and network filters were empty; active container and network counts returned to their pre-run values of 11 and 12.

This is a bounded synthetic run. It does not qualify full-volume behavior, concurrent clients or failpoints, and `historical_phase_b_replayed` is false. The hosted static walkthrough remains a separate product surface and never runs SQL.

## Refusal, rollback and return

Every failed identity check is a stop condition. A manifest, config, platform or BuildKit-material mismatch from the build verifier means the candidate image is not loaded; keep the V2 and V1 pins unchanged. A package-provenance or preflight failure means no rehearsal resources were created. Preserve the command output and correct the environment before restarting from the matching verification step.

The deliberate `invalid-negative-amount` scenario exits with `Amount must be a positive whole number of cents.` before Docker preflight or resource creation. It demonstrates rejection; it is not a migration failure. Do not use it as the successful result.

The `small-shop` runner itself performs rollback to the legacy shape and then roll-forward through contract before returning JSON. A successful result has `verdict: LOCAL_REHEARSAL_PASSED`, `rollback.legacy_rows_restored: true`, `rollback.roll_forward_complete: true`, `cleanup_state: complete`, and `owned_resources_remaining: 0`. It also states `historical_phase_b_replayed: false`. No second rollback command is needed.

After an error or successful run, inspect only this run's V2 name prefix:

```sh
docker ps -a --filter name=pmr-demo-v2-
docker network ls --filter name=pmr-demo-v2-
```

Both listings should be empty after cleanup. If a listing is not empty, stop, preserve the result and inspect the exact resource labels/IDs before taking action; do not remove unrelated containers, networks or the shared reviewed image. Once the preflight passes and the resource listings are empty, rerun `small-shop` to return to a clean successful result.
