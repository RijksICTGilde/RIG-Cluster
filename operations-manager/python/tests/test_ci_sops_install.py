"""De CI-testjob installeert sops, op de versie van de Dockerfile, voor de testrun.

Plus de andere kant van diezelfde afspraak: welke crypto-binaries de image meelevert.
"""

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


INSTALL_STAP = "Install sops"


def _index_of(steps: list[dict], pattern: str) -> int:
    hits = [i for i, step in enumerate(steps) if re.search(pattern, step.get("run", ""))]
    assert hits, f"geen stap in jobs.test die {pattern!r} draait"
    return hits[0]


def _index_of_name(steps: list[dict], name: str) -> int:
    """De stap op naam, niet op de commando's erin.

    Hoe sops op zijn plek komt (curl + install, of curl + mv) is de stap zijn zaak; deze toets gaat
    over de volgorde en de versie. Een guard die op de spelling van het commando let, gaat stuk bij
    een verbetering die niets verandert aan wat hij bewaakt.
    """
    hits = [i for i, step in enumerate(steps) if step.get("name") == name]
    assert hits, f"geen stap in jobs.test met naam {name!r}"
    return hits[0]


def test_sops_is_installed_before_the_tests_run(ci_test_steps: list[dict]) -> None:
    install = _index_of_name(ci_test_steps, INSTALL_STAP)
    pytest_run = _index_of(ci_test_steps, r"\bpytest\b")
    assert install < pytest_run, "sops wordt pas na de testrun geinstalleerd, de sops-tests slaan dan over"


def test_sops_version_matches_the_dockerfile(ci_test_steps: list[dict]) -> None:
    dockerfile_version = re.search(r"^ARG SOPS_VERSION=(\S+)$", DOCKERFILE.read_text(), re.MULTILINE)
    assert dockerfile_version, "ARG SOPS_VERSION ontbreekt in de Dockerfile"
    script = ci_test_steps[_index_of_name(ci_test_steps, INSTALL_STAP)]["run"]
    ci_versions = set(re.findall(r"/releases/download/(v[\d.]+)/sops-(v[\d.]+)\.", script))
    assert ci_versions == {(dockerfile_version[1], dockerfile_version[1])}, (
        f"CI haalt sops {sorted(ci_versions)}, de Dockerfile pint {dockerfile_version[1]}"
    )


def _apt_pakketten(dockerfile: str) -> set[str]:
    """De pakketnamen uit elke ``apt-get install`` in de Dockerfile, een eventuele versiepin eraf."""
    doorlopend = re.sub(r"\\\n\s*", " ", dockerfile)
    namen: set[str] = set()
    for blok in re.findall(r"apt-get install\s+(.*?)(?:&&|$)", doorlopend, re.MULTILINE):
        for woord in blok.split():
            if not woord.startswith("-"):
                namen.add(woord.split("=")[0])
    return namen


def test_de_image_installeert_het_age_binary_nog() -> None:
    """Ontsleutelen loopt sinds RC-218 in het proces, maar versleutelen en SOPS draaien nog op
    het binary; zie features/age-ontsleuteling-in-proces.md. Zonder deze toets is het een
    voor de hand liggend pakket om weg te halen bij de volgende opruimronde van de image.
    """
    pakketten = _apt_pakketten(DOCKERFILE.read_text())
    assert "age" in pakketten, f"de Dockerfile installeert age niet meer, wel {sorted(pakketten)}"
