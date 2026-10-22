#!/usr/bin/env python3
"""Run the separately versioned PMR-DEMO-20261022-v2 local baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import rehearse_baseline_v1 as runtime

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "runtime-baselines/demo-20261022-v2/baseline.json"
BASELINE_SHA256 = "e317bbab8e8dc8e9db63272990da4094fcf344eda4dc62ebcdc915b3e84f7c39"
BASELINE_VERSION = "PMR-DEMO-20261022-v2"
RESOURCE_PREFIX = "pmr-demo-v2-"


def preflight() -> dict:
    return runtime.preflight(
        baseline_path=BASELINE,
        baseline_sha256=BASELINE_SHA256,
        expected_version=BASELINE_VERSION,
    )


def rehearse(scenario: str) -> dict:
    return runtime.rehearse(
        scenario,
        baseline_path=BASELINE,
        baseline_sha256=BASELINE_SHA256,
        expected_version=BASELINE_VERSION,
        resource_prefix=RESOURCE_PREFIX,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=["small-shop", "empty-ledger", "invalid-negative-amount"],
        default="small-shop",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="verify exact source and runtime identity without creating resources",
    )
    args = parser.parse_args()
    if args.preflight_only:
        baseline = preflight()
        result = {
            "preflight": "passed",
            "baseline": baseline["version"],
            "baseline_image": baseline["image_manifest"],
            "resources_created": False,
        }
    else:
        result = rehearse(args.scenario)
    print(json.dumps(result, sort_keys=True, indent=2))
