"""The portable baseline remains separate from the frozen V1 and browser pins."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "demo_baseline_v2", ROOT / "scripts/rehearse_baseline_v2.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
BUILD_SPEC = importlib.util.spec_from_file_location(
    "demo_baseline_v2_build", ROOT / "scripts/verify_demo_baseline_v2_build.py"
)
assert BUILD_SPEC is not None and BUILD_SPEC.loader is not None
BUILD_MODULE = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(BUILD_MODULE)
PROVENANCE_SPEC = importlib.util.spec_from_file_location(
    "docker_repository_provenance", ROOT / "scripts/verify_docker_repository_provenance.py"
)
assert PROVENANCE_SPEC is not None and PROVENANCE_SPEC.loader is not None
PROVENANCE_MODULE = importlib.util.module_from_spec(PROVENANCE_SPEC)
PROVENANCE_SPEC.loader.exec_module(PROVENANCE_MODULE)
BASELINE = json.loads((ROOT / "runtime-baselines/demo-20261022-v2/baseline.json").read_text())
V1 = json.loads((ROOT / "runtime-baselines/demo-20260908/baseline.json").read_text())


def test_v2_manifest_is_exact_and_keeps_v1_image_and_source_pins() -> None:
    raw = (ROOT / "runtime-baselines/demo-20261022-v2/baseline.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == MODULE.BASELINE_SHA256
    assert BASELINE["version"] == MODULE.BASELINE_VERSION
    assert BASELINE["derived_from"]["baseline_sha256"] == (
        "feb4ce482d64d4331d080a5c518ecdc8bc716e810f2e57e486a942928be50dd4"
    )
    assert BASELINE["image_manifest"] == V1["image_manifest"]
    assert BASELINE["image_config"] == V1["image_config"]
    assert BASELINE["source_sha256"] == V1["source_sha256"]


def test_v2_preflight_uses_its_own_reviewed_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def fake_preflight(**kwargs: object) -> dict:
        observed.update(kwargs)
        return BASELINE

    monkeypatch.setattr(MODULE.runtime, "preflight", fake_preflight)
    assert MODULE.preflight() is BASELINE
    assert observed == {
        "baseline_path": MODULE.BASELINE,
        "baseline_sha256": MODULE.BASELINE_SHA256,
        "expected_version": MODULE.BASELINE_VERSION,
    }


def test_v2_rehearsal_uses_a_distinct_resource_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def fake_rehearse(scenario: str, **kwargs: object) -> dict:
        observed["scenario"] = scenario
        observed.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(MODULE.runtime, "rehearse", fake_rehearse)
    assert MODULE.rehearse("small-shop") == {"ok": True}
    assert observed["scenario"] == "small-shop"
    assert observed["resource_prefix"] == "pmr-demo-v2-"
    assert observed["expected_version"] == MODULE.BASELINE_VERSION


def test_build_verifier_accepts_only_the_reviewed_image_and_materials(
    tmp_path: Path,
) -> None:
    metadata = {
        "containerimage.digest": BASELINE["image_manifest"],
        "containerimage.config.digest": BASELINE["image_config"],
        "containerimage.descriptor": {"platform": {"architecture": "amd64", "os": "linux"}},
        "buildx.build.provenance": {
            "materials": [
                {"uri": item["uri"], "digest": {"sha256": item["sha256"]}}
                for item in BASELINE["build_materials"]
            ]
        },
    }
    path = tmp_path / "build.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    assert BUILD_MODULE.verify_build(path)["materials_verified"] == 3

    metadata["containerimage.digest"] = "sha256:other"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(RuntimeError, match="manifest differs"):
        BUILD_MODULE.verify_build(path)


def test_docker_provenance_verifier_parses_signed_package_stanzas() -> None:
    package = PROVENANCE_MODULE.parse_fields(
        "Package: docker-ce-cli\nVersion: 5:29.8.2-1~ubuntu.24.04~noble\n"
        "Filename: pool/stable/amd64/docker-ce-cli.deb\n"
        "SHA256: abc123\nDescription: first line\n second line\n"
    )
    assert package["Package"] == "docker-ce-cli"
    assert package["SHA256"] == "abc123"
    assert package["Description"] == "first line\nsecond line"


def test_v1_raw_baseline_pin_and_historical_browser_identity_are_unchanged() -> None:
    raw_v1 = (ROOT / "runtime-baselines/demo-20260908/baseline.json").read_bytes()
    assert hashlib.sha256(raw_v1).hexdigest() == MODULE.runtime.BASELINE_SHA256
    from migration_rehearsal.control_room_adapter import _IMAGE_ID

    assert _IMAGE_ID == "sha256:e4155efcf0c7e302168f98a9db891ff4ed14ff3209244308533332d30cd7f6ee"
