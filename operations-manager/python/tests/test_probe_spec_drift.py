"""Drift guard for the e2e-allservices probe spec.

``images/e2e-allservices/probe_spec.json`` is generated from OPI's service
registry by ``scripts/generate_probe_spec.py`` and embedded in the test-image
binary. If a service or an injected variable is added but the spec is not
regenerated, the image would silently stop testing it. These tests fail in that
case, forcing a regenerate, and also assert the spec's structural invariants.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GENERATOR = _REPO_ROOT / "scripts" / "generate_probe_spec.py"
_SPEC_FILE = _REPO_ROOT / "images" / "e2e-allservices" / "probe_spec.json"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_probe_spec", _GENERATOR)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    return _load_generator()


def test_committed_spec_matches_registry(generator: ModuleType) -> None:
    """The committed spec is exactly what the generator produces now.

    If this fails, the service registry changed without regenerating the spec:
        uv run python scripts/generate_probe_spec.py
    """
    expected = generator.render(generator.build_spec())
    actual = _SPEC_FILE.read_text()
    assert actual == expected, (
        "probe_spec.json is out of date with the service registry. "
        "Regenerate it: uv run python scripts/generate_probe_spec.py"
    )


def test_spec_check_mode_passes(generator: ModuleType) -> None:
    """The generator's own --check logic agrees the committed file is current."""
    expected = generator.render(generator.build_spec())
    assert _SPEC_FILE.read_text() == expected


def test_every_service_with_vars_is_covered(generator: ModuleType) -> None:
    """No service that injects variables is left unmapped (would be untested)."""
    from opi.services.services import ServiceAdapter
    from opi.services.services_enums import ServiceType

    for service in ServiceType:
        definition = ServiceAdapter.SERVICE_DEFINITIONS.get(service)
        if definition is None or not definition.variables:
            continue
        assert service in generator.KIND_MAP, (
            f"Service '{service.value}' injects variables but is not mapped to a probe kind "
            f"in scripts/generate_probe_spec.py (KIND_MAP)."
        )


def test_spec_targets_are_well_formed(generator: ModuleType) -> None:
    """Structural invariants the Go loader relies on."""
    spec = generator.build_spec()
    known_kinds = {"sql", "redis", "s3", "oidc", "path", "metadata"}
    ids: set[str] = set()
    for target in spec["targets"]:
        assert target["id"] not in ids, f"duplicate target id {target['id']!r}"
        ids.add(target["id"])
        assert target["kind"] in known_kinds, f"unknown probe kind {target['kind']!r}"
        assert target["services"], f"target {target['id']!r} has no contributing services"
        assert target["env_vars"], f"target {target['id']!r} has no env vars"
        # Every boundness var must also be one of the target's env vars.
        assert set(target["bound_when_any"]).issubset(set(target["env_vars"])), (
            f"target {target['id']!r} bound_when_any not a subset of env_vars"
        )


def test_computed_keys_present(generator: ModuleType) -> None:
    """The scan picks up computed secret keys, not just the enum's canonical vars."""
    spec = generator.build_spec()
    by_id = {t["id"]: t for t in spec["targets"]}
    assert "DATABASE_SERVER_FULL" in by_id["postgres"]["env_vars"]
    assert "OBJECT_STORE_ENDPOINT_URL" in by_id["minio"]["env_vars"]
    assert "REDIS_URL" in by_id["redis"]["env_vars"]
