# PMR-DEMO-20261022-v2

This is a new local baseline for the six-invoice CLI rehearsal. It keeps the 2026-09-08 V1 manifest, source hashes and image digest unchanged, and it does not replay or repair historical Phase B or the browser image. It records a current, authenticated Docker toolchain and a fresh build that reproduced the V1 image identity.

## Qualified environment

The recorded run used WSL 2, Ubuntu 24.04.5 LTS, Linux amd64, Python 3.12.14 and uv 0.11.7. Docker CLI and Engine were 29.8.2; Buildx was 0.37.1 with BuildKit 0.33.1. The required Docker context is `default` at `unix:///var/run/docker.sock`.

The exact Docker CLI, Engine and Buildx package versions, signed repository metadata, archive digests and installed executable digests are in [provenance.json](provenance.json). The retained [InRelease](docker-repository/InRelease), [Docker signing key](docker-repository/docker.asc) and [amd64 Packages index](docker-repository/Packages) let the signature-to-binary chain be checked independently. The verifier validates the signing fingerprints and signature, confirms the signed index digest and size, checks each exact version and archive digest against the index, downloads only those bounded exact packages when local `.deb` files are not supplied, extracts them in a temporary directory, and checks the three executable digests. Its recorded output is [package-verification.json](package-verification.json). `dpkg -V` also passed for all three installed packages. The V2 preflight repeats the installed package version and executable hash checks before creating resources.

## Build and run

Start from a fresh WSL-native checkout as described in the [V1 procedure](../demo-20260908/README.md#windows-with-wsl-2). Keep the checkout in the WSL filesystem and preserve LF bytes for the pinned source files. The V2 baseline expects the exact Python, Docker CLI, Engine and Buildx versions recorded above; do not edit its hashes to accept a different runtime.

Install the locked Python environment, then build a temporary OCI archive. The verifier checks the manifest, config, platform and all three recorded BuildKit materials before the archive is loaded:

```sh
uv python install 3.12.14
uv sync --frozen
uv run --frozen python --version
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

This is a bounded synthetic run. It does not qualify full-volume behavior, concurrent clients or failpoints, and `historical_phase_b_replayed` is false. The hosted static walkthrough remains a separate product surface.
