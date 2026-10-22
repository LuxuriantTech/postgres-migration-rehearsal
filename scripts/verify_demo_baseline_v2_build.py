#!/usr/bin/env python3
"""Check Buildx metadata against the immutable PMR-DEMO-20261022-v2 inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "runtime-baselines/demo-20261022-v2/baseline.json"
BASELINE_SHA256 = "e317bbab8e8dc8e9db63272990da4094fcf344eda4dc62ebcdc915b3e84f7c39"


def verify_build(metadata_path: Path) -> dict[str, object]:
    baseline_raw = BASELINE.read_bytes()
    if hashlib.sha256(baseline_raw).hexdigest() != BASELINE_SHA256:
        raise RuntimeError("reviewed V2 baseline changed")
    baseline = json.loads(baseline_raw)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("containerimage.digest") != baseline["image_manifest"]:
        raise RuntimeError("build output manifest differs from the reviewed V2 image")
    if metadata.get("containerimage.config.digest") != baseline["image_config"]:
        raise RuntimeError("build output config differs from the reviewed V2 image")
    descriptor = metadata.get("containerimage.descriptor", {})
    if descriptor.get("platform") != {"architecture": "amd64", "os": "linux"}:
        raise RuntimeError("build output platform differs from linux/amd64")
    provenance = metadata.get("buildx.build.provenance", {})
    materials = provenance.get("materials", [])
    actual_materials = sorted(
        (
            {"uri": item.get("uri"), "sha256": item.get("digest", {}).get("sha256")}
            for item in materials
        ),
        key=lambda item: (item["uri"] or "", item["sha256"] or ""),
    )
    expected_materials = sorted(
        baseline["build_materials"], key=lambda item: (item["uri"], item["sha256"])
    )
    if actual_materials != expected_materials:
        raise RuntimeError("build materials differ from the reviewed V2 inputs")
    return {
        "build": "passed",
        "image_manifest": baseline["image_manifest"],
        "image_config": baseline["image_config"],
        "platform": baseline["platform"],
        "materials_verified": len(actual_materials),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata_file", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify_build(args.metadata_file), sort_keys=True, indent=2))
