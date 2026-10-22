# Independent local demo baseline, 2026-09-08

This is PMR-DEMO-20260908-v1. It does not replace the frozen browser image or historical Phase B. The browser still rejects its unavailable original image. This separate CLI runs the existing six-invoice migration, rollback and roll-forward engine in a newly created internal Docker network, with a loopback-only relay and disposable database. It does not expose an HTTP endpoint.

## Build and verify

Requires Linux amd64, local Docker Engine/BuildKit with OCI exporter support, Python 3.12.14 and uv. The Docker executable and source files must match baseline.json. Different environments stop for review; do not edit the pin merely to accept another image.

### Windows with WSL 2

Keep the Linux checkout in the WSL filesystem (for example, under `~/src`) so Linux file hashes and Docker bind mounts use the same files. Clone from inside WSL with `core.autocrlf` disabled, then run every command below in that same distribution from the repository root:

```sh
mkdir -p ~/src
cd ~/src
git -c core.autocrlf=false clone https://github.com/LuxuriantTech/postgres-migration-rehearsal.git
cd postgres-migration-rehearsal
```

The repository enforces LF for baseline-pinned JSON, Python, SQL and Dockerfile inputs so Windows Git settings cannot silently change their checked-out bytes. Use a fresh checkout after updating to this release; an older Windows checkout may already contain CRLF versions of those files.

Windows Docker Desktop integration is supported only when WSL's `/usr/bin/docker` reaches the local engine through context `default` at `unix:///var/run/docker.sock`. Check the environment before creating build output:

```sh
uname -m                         # x86_64
uv --version
docker context show              # default
docker context inspect default --format '{{.Endpoints.docker.Host}}'
sha256sum /usr/bin/docker
```

The last command must report `a429e235ef670ea83357a5c8c7451f0a69d485a6fee49f9032fd938a0ab4969d`. The runner checks the full digest and prints expected and observed hashes on a mismatch. A refusal means this runtime has not been qualified for this baseline; keep the pin and stop. Do not substitute the Windows `docker.exe`, another Docker context, or a different image. WSL path access is sufficient for the checkout; the Linux build and rehearsal run inside WSL.

After those read-only runtime checks pass, install Python 3.12.14 with uv, sync the frozen environment and confirm its interpreter. The following uses a newly created temporary directory, so earlier output files are preserved:

```sh
uv python install 3.12.14
uv sync --frozen
uv run --frozen python --version  # Python 3.12.14
pmr_tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/pmr-demo-v1.XXXXXX")"
trap 'rm -rf "$pmr_tmpdir"' EXIT
docker buildx build --platform linux/amd64 --no-cache --provenance=false \
  --build-arg SOURCE_DATE_EPOCH=1788825600 \
  --output "type=oci,rewrite-timestamp=true,dest=$pmr_tmpdir/image.oci.tar" \
  --metadata-file "$pmr_tmpdir/build.json" runtime-baselines/demo-20260908
docker load -i "$pmr_tmpdir/image.oci.tar"
PYTHONPATH=src .venv/bin/python scripts/rehearse_baseline_v1.py
```

The runner completes all baseline, Docker executable, source and image checks before it creates a network or container. It creates only a uniquely named internal network and database container carrying its own random ownership label, then removes those exact IDs and confirms no owned resources remain. It never cleans up unrelated Docker resources. If the runner stops with an error, preserve its output and inspect only resources carrying that run's `pmr.demo` label before taking any recovery action.

```sh
uv sync --frozen
docker buildx build --platform linux/amd64 --no-cache --provenance=false \
  --build-arg SOURCE_DATE_EPOCH=1788825600 \
  --output type=oci,rewrite-timestamp=true,dest=/tmp/pmr-demo-v1.oci.tar \
  --metadata-file /tmp/pmr-demo-v1.build.json runtime-baselines/demo-20260908
docker load -i /tmp/pmr-demo-v1.oci.tar
PYTHONPATH=src .venv/bin/python scripts/rehearse_baseline_v1.py
```

Choose unused output filenames to retain earlier builds. The OCI manifest must be `sha256:1a5aa9ff157a2390e11e5e30965be8c4f1d50ea14264f2dd4f9c38bd4ad7cbbd`. Two no-cache builds on the observed BuildKit environment produced this manifest and configuration `sha256:d3b733054757a7473a37b8f9f8f5fd5d60dddb5b2f3a54260f9d866daba4cd18`.

The original Dockerfile has pinned PostgreSQL and APK inputs, but APK writes wall-clock timestamps into its log. Two timestamp-normalized builds differed only in that log. This new Dockerfile clears the build log in the same layer before export; build transcripts remain separate evidence. Clearing it in a later layer does not make earlier layers reproducible. Attestations are excluded from image identity for this baseline; build material metadata is retained separately. The original frozen identity cannot be reconstructed from these observations: its old artifact is unavailable and its pre-rebuild identity was not recorded.

## Result and limits

The six synthetic rows retained their values, both idempotent backfill passes changed zero rows, rollback restored the legacy data, and roll-forward completed through the contract phase. The CLI reports cleanup only after deleting its own verified resource IDs. Invalid negative data is rejected before Docker starts. This is a new bounded demonstration, not the old Phase B, a concurrent-client exercise, production acceptance or a security certification. Same-user and Docker-administrator mutation remain outside this local trust boundary.
