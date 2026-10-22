#!/usr/bin/env python3
"""Reproduce the Docker apt signature, package, and installed-binary checks."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "runtime-baselines/demo-20261022-v2/docker-repository"
BASELINE = ROOT / "runtime-baselines/demo-20261022-v2/baseline.json"
INRELEASE_SHA256 = "bee03aa9b5f1939728cee4a72c670128f96b7b034a14f4700bdc356888235974"
PACKAGES_SHA256 = "140fe286549dfb9ad196c7b92257e8bcfe0df88f7b27e4fbe2cf9800c8a150a4"
PACKAGES_SIZE = 476337
PRIMARY_FINGERPRINT = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
SIGNING_FINGERPRINT = "D3306A018370199E527AE7997EA0A9C3F273FCD8"
REPOSITORY = "https://download.docker.com/linux/ubuntu"
PACKAGES_PATH = "stable/binary-amd64/Packages"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_fields(stanza: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current = ""
    for line in stanza.splitlines():
        if line[:1].isspace() and current:
            fields[current] += "\n" + line.strip()
        elif ": " in line:
            current, value = line.split(": ", 1)
            fields[current] = value
    return fields


def run(args: list[str]) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=30)


def bounded_copy(source: io.BufferedReader, destination: io.BufferedWriter) -> None:
    total = 0
    while chunk := source.read(1024 * 1024):
        total += len(chunk)
        if total > 30_000_000:
            raise ValueError("refusing package download larger than 30 MB")
        destination.write(chunk)


def verify(deb_dir: Path | None = None) -> dict:
    inrelease = EVIDENCE / "InRelease"
    packages = EVIDENCE / "Packages"
    key = EVIDENCE / "docker.asc"
    for path, expected in (
        (inrelease, INRELEASE_SHA256),
        (packages, PACKAGES_SHA256),
        (key, "1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570"),
    ):
        if sha256(path) != expected:
            raise ValueError(f"evidence digest mismatch: {path.name}")

    with tempfile.TemporaryDirectory(prefix="pmr-docker-proof-") as temporary:
        temp = Path(temporary)
        dearmored = temp / "docker.gpg"
        run(["gpg", "--batch", "--yes", "--dearmor", "--output", str(dearmored), str(key)])
        key_listing = run(["gpg", "--batch", "--show-keys", "--with-colons", str(key)])
        fingerprints = [
            line.split(":")[9] for line in key_listing.splitlines() if line.startswith("fpr:")
        ]
        if PRIMARY_FINGERPRINT not in fingerprints or SIGNING_FINGERPRINT not in fingerprints:
            raise ValueError("Docker signing key fingerprint mismatch")
        release_path = temp / "Release"
        signature = run(
            ["gpgv", "--keyring", str(dearmored), "--output", str(release_path), str(inrelease)]
        )

        release = release_path.read_text(encoding="utf-8")
        signed_index = any(
            line.split() == [PACKAGES_SHA256, str(PACKAGES_SIZE), PACKAGES_PATH]
            for line in release.splitlines()
        )
        if not signed_index:
            raise ValueError("signed InRelease does not authenticate the retained Packages index")
        if packages.stat().st_size != PACKAGES_SIZE:
            raise ValueError("Packages index size differs from signed InRelease")

        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        stanzas = [
            parse_fields(item)
            for item in packages.read_text(encoding="utf-8").split("\n\n")
            if item.strip()
        ]
        verified = []
        packages_expected = [
            (baseline["docker_package"], baseline["docker_sha256"], "/usr/bin/docker"),
            (baseline["engine_package"], baseline["engine_sha256"], "/usr/bin/dockerd"),
            (
                baseline["buildx_package"],
                baseline["buildx_sha256"],
                "/usr/libexec/docker/cli-plugins/docker-buildx",
            ),
        ]
        for package_entry, expected_binary_hash, binary_path in packages_expected:
            expected = {
                **package_entry,
                "binary_sha256": expected_binary_hash,
                "binary_path": binary_path,
            }
            matches = [
                item
                for item in stanzas
                if item.get("Package") == expected["name"]
                and item.get("Version") == expected["version"]
                and item.get("Architecture") == "amd64"
            ]
            if len(matches) != 1:
                raise ValueError(f"expected one signed package stanza for {expected['name']}")
            package = matches[0]
            if package["SHA256"] != expected["archive_sha256"]:
                raise ValueError(f"signed package digest mismatch for {expected['name']}")
            filename = package["Filename"]
            if (
                not filename.startswith("dists/noble/pool/stable/amd64/")
                or Path(filename).name != filename.rsplit("/", 1)[-1]
            ):
                raise ValueError(f"unexpected package path for {expected['name']}")
            archive = (deb_dir / Path(filename).name) if deb_dir else temp / Path(filename).name
            if not archive.exists():
                if deb_dir:
                    raise ValueError(
                        f"exact package archive not found in --deb-dir: {archive.name}"
                    )
                url = f"{REPOSITORY}/{filename}"
                request = urllib.request.Request(
                    url, headers={"User-Agent": "pmr-provenance-verifier/1"}
                )
                with (
                    urllib.request.urlopen(request, timeout=45) as response,
                    archive.open("wb") as output,
                ):
                    length = response.headers.get("Content-Length")
                    if length is not None and int(length) > 30_000_000:
                        raise ValueError(
                            f"refusing oversized package download for {expected['name']}"
                        )
                    bounded_copy(response, output)
            if archive.stat().st_size > 30_000_000:
                raise ValueError(f"refusing oversized package archive for {expected['name']}")
            if sha256(archive) != package["SHA256"]:
                raise ValueError(f"downloaded package digest mismatch for {expected['name']}")
            extract = temp / expected["name"]
            extract.mkdir()
            run(["dpkg-deb", "-x", str(archive), str(extract)])
            binary = extract / expected["binary_path"].lstrip("/")
            if sha256(binary) != expected["binary_sha256"]:
                raise ValueError(f"binary digest mismatch for {expected['name']}")
            verified.append(
                {
                    "package": expected["name"],
                    "version": expected["version"],
                    "archive_sha256": package["SHA256"],
                    "binary_sha256": sha256(binary),
                    "status": "verified",
                }
            )

    return {
        "status": "verified",
        "signature": signature.strip(),
        "signed_index": PACKAGES_PATH,
        "signed_index_sha256": PACKAGES_SHA256,
        "packages": verified,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deb-dir",
        type=Path,
        help="use previously downloaded exact .deb files from this directory",
    )
    print(json.dumps(verify(parser.parse_args().deb_dir), indent=2, sort_keys=True))
