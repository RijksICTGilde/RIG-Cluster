"""De CI-testjob installeert sops, op de versie van de Dockerfile, voor de testrun."""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DOCKERFILE = REPO_ROOT / "operations-manager" / "Dockerfile"


@pytest.fixture(scope="module")
def ci_test_steps() -> list[dict]:
    return yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["test"]["steps"]


def _index_of(steps: list[dict], pattern: str) -> int:
    hits = [i for i, step in enumerate(steps) if re.search(pattern, step.get("run", ""))]
    assert hits, f"geen stap in jobs.test die {pattern!r} draait"
    return hits[0]


def test_sops_is_installed_before_the_tests_run(ci_test_steps: list[dict]) -> None:
    install = _index_of(ci_test_steps, r"install .*/sops\b")
    pytest_run = _index_of(ci_test_steps, r"\bpytest\b")
    assert install < pytest_run, "sops wordt pas na de testrun geinstalleerd, de sops-tests slaan dan over"


def test_sops_version_matches_the_dockerfile(ci_test_steps: list[dict]) -> None:
    dockerfile_version = re.search(r"^ARG SOPS_VERSION=(\S+)$", DOCKERFILE.read_text(), re.MULTILINE)
    assert dockerfile_version, "ARG SOPS_VERSION ontbreekt in de Dockerfile"
    script = ci_test_steps[_index_of(ci_test_steps, r"install .*/sops\b")]["run"]
    ci_versions = set(re.findall(r"/releases/download/(v[\d.]+)/sops-(v[\d.]+)\.", script))
    assert ci_versions == {(dockerfile_version[1], dockerfile_version[1])}, (
        f"CI haalt sops {sorted(ci_versions)}, de Dockerfile pint {dockerfile_version[1]}"
    )
