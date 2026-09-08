"""The new baseline must reject drift without changing the frozen adapter."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "demo_baseline", ROOT / "scripts/rehearse_baseline_v1.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
BASELINE = json.loads((ROOT / "runtime-baselines/demo-20260908/baseline.json").read_text())


def test_exact_reviewed_image_identity() -> None:
    MODULE.verify_image(
        {
            "Id": BASELINE["image_manifest"],
            "Architecture": "amd64",
            "Os": "linux",
            "Config": {"User": "70:70"},
        },
        BASELINE,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("Id", "different-image"),
        ("Architecture", "arm64"),
        ("Os", "other"),
        ("Config", {"User": "root"}),
    ],
)
def test_image_drift_is_rejected(field: str, value: object) -> None:
    image = {
        "Id": BASELINE["image_manifest"],
        "Architecture": "amd64",
        "Os": "linux",
        "Config": {"User": "70:70"},
    }
    image[field] = value
    with pytest.raises(RuntimeError, match="identity mismatch"):
        MODULE.verify_image(image, BASELINE)


def test_invalid_fixture_never_starts_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args: object) -> str:
        pytest.fail("invalid fixture reached Docker")

    monkeypatch.setattr(MODULE, "docker", unexpected)
    with pytest.raises(ValueError):
        MODULE.rehearse("invalid-negative-amount")


def test_historical_browser_pin_is_preserved() -> None:
    from migration_rehearsal.control_room_adapter import _IMAGE_ID

    assert _IMAGE_ID == "sha256:e4155efcf0c7e302168f98a9db891ff4ed14ff3209244308533332d30cd7f6ee"
    assert BASELINE["image_manifest"] != _IMAGE_ID


def test_changed_manifest_rejected_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "baseline.json"
    changed = dict(BASELINE, image_manifest="unreviewed-image")
    path.write_text(json.dumps(changed))
    monkeypatch.setattr(MODULE, "BASELINE", path)
    with pytest.raises(RuntimeError, match="manifest changed"):
        MODULE.preflight()


def test_network_create_timeout_recovers_owned_id(monkeypatch: pytest.MonkeyPatch) -> None:
    nonce = "1234567890abcdef"
    calls = []
    monkeypatch.setattr(MODULE, "preflight", lambda: BASELINE)
    monkeypatch.setattr(MODULE.secrets, "token_hex", lambda _: nonce)

    def fake_docker(*args: str) -> str:
        calls.append(args)
        if args[:2] == ("network", "create"):
            raise subprocess.TimeoutExpired("docker", 30)
        if args[:2] == ("container", "inspect"):
            raise subprocess.CalledProcessError(1, "docker")
        if args[:2] == ("network", "inspect"):
            return json.dumps(
                [{"Id": "owned-network", "Labels": {"pmr.demo": nonce}, "Containers": {}}]
            )
        if args == ("network", "rm", "owned-network"):
            return "owned-network"
        pytest.fail("unexpected Docker operation")

    monkeypatch.setattr(MODULE, "docker", fake_docker)
    with pytest.raises(subprocess.TimeoutExpired):
        MODULE.rehearse("small-shop")
    assert ("network", "rm", "owned-network") in calls


def test_recovery_does_not_adopt_foreign_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        MODULE,
        "docker",
        lambda *args: json.dumps([{"Id": "foreign", "Labels": {"pmr.demo": "other"}}]),
    )
    with pytest.raises(RuntimeError, match="ownership differs"):
        MODULE.recover_owned("network", "pmr-demo-v1-example", "1234567890abcdef")
