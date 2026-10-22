"""CODEOWNERS en SECURITY.md blijven in de pas met de repo.

Een hernoemd gevoelig pad raakt zo niet stil uit beeld van zijn eigenaar.
"""

import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CODEOWNERS = REPO_ROOT / ".github" / "CODEOWNERS"
SECURITY = REPO_ROOT / "SECURITY.md"

GEVOELIGE_BESTANDEN = [
    "operations-manager/python/opi/middleware/authorization.py",
    "operations-manager/python/opi/middleware/security_headers.py",
    "operations-manager/python/opi/api/user_token_auth.py",
    "operations-manager/python/opi/services/project_authorization.py",
    "operations-manager/python/opi/utils/api_keys.py",
    "operations-manager/python/opi/utils/age.py",
    "operations-manager/python/opi/utils/sops.py",
    "operations-manager/python/opi/utils/logging_redact.py",
    "operations-manager/python/opi/core/actor.py",
    "operations-manager/python/opi/utils/git_signing.py",
    "operations-manager/python/manifests/deployment.yaml.jinja",
    "bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml",
    "infrastructure/bootstrap/infrastructure/kustomization.yaml",
    ".github/workflows/security.yml",
    ".github/CODEOWNERS",
    "SECURITY.md",
]

SECURITY_KOPPEN = [
    "## Een kwetsbaarheid melden",
    "## Wat je van ons mag verwachten",
    "## Hoe we beveiliging geregeld hebben",
    "## Versies",
]

PVR_LINK = "https://github.com/RijksICTGilde/RIG-Cluster/security/advisories/new"
EMAIL = re.compile(r"@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _regels() -> list[tuple[str, list[str]]]:
    """De patroon-plus-eigenaren van elke inhoudelijke regel in CODEOWNERS."""
    regels = []
    for regel in CODEOWNERS.read_text().splitlines():
        kaal = regel.split("#", 1)[0].strip()
        if not kaal:
            continue
        velden = kaal.split()
        regels.append((velden[0], velden[1:]))
    return regels


@pytest.fixture(scope="module")
def gevolgde_bestanden() -> list[str]:
    uitvoer = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return uitvoer.stdout.splitlines()


def _raakt(patroon: str, pad: str) -> bool:
    """Of een CODEOWNERS-patroon dit pad dekt.

    Alleen de vorm die wij gebruiken: een vanaf de root geankerd bestand of map, waarin een
    `*` net als in gitignore binnen een segment blijft. De toets hieronder houdt die
    beperking vast, zodat deze matcher niet stil verkeerd gaat gelden voor een breder patroon.
    """
    patroondelen = patroon.strip("/").split("/")
    paddelen = pad.split("/")
    if len(paddelen) < len(patroondelen):
        return False
    voorloop = paddelen[: len(patroondelen)]
    return all(fnmatch(deel, patroondeel) for patroondeel, deel in zip(patroondelen, voorloop, strict=True))


def test_elk_patroon_is_geankerd_en_blijft_binnen_een_segment() -> None:
    for patroon, _ in _regels():
        assert patroon.startswith("/"), f"{patroon} is niet vanaf de root geankerd"
        assert not set(patroon) & set("?[]"), f"{patroon} gebruikt een patroonteken dat wij niet aankunnen"
        assert "**" not in patroon, f"{patroon} gebruikt een wildcard over mapgrenzen heen"


def test_elke_regel_heeft_een_eigenaar() -> None:
    for patroon, eigenaren in _regels():
        assert eigenaren, f"{patroon} heeft geen eigenaar"
        for eigenaar in eigenaren:
            assert eigenaar.startswith("@"), f"{eigenaar} bij {patroon} is geen GitHub-handle"


def test_elk_patroon_raakt_een_bestaand_bestand(gevolgde_bestanden: list[str]) -> None:
    for patroon, _ in _regels():
        assert any(_raakt(patroon, pad) for pad in gevolgde_bestanden), (
            f"{patroon} raakt geen enkel bestand in git; hernoemd of verwijderd?"
        )


@pytest.mark.parametrize("pad", GEVOELIGE_BESTANDEN)
def test_gevoelig_bestand_heeft_een_eigenaar(pad: str) -> None:
    assert (REPO_ROOT / pad).exists(), f"{pad} bestaat niet meer; werk deze lijst bij"
    assert any(_raakt(patroon, pad) for patroon, _ in _regels()), (
        f"{pad} wordt door geen enkel CODEOWNERS-patroon gedekt"
    )


def test_security_md_heeft_de_koppen_en_de_meldweg() -> None:
    tekst = SECURITY.read_text()
    for kop in SECURITY_KOPPEN:
        assert kop in tekst, f"kop {kop!r} ontbreekt"
    assert PVR_LINK in tekst


def test_security_md_noemt_geen_e_mailadres() -> None:
    treffer = EMAIL.search(SECURITY.read_text())
    assert treffer is None, f"e-mailadres in SECURITY.md: {treffer.group(0) if treffer else ''}"


@pytest.mark.parametrize(
    "pad",
    [REPO_ROOT / "README.md", REPO_ROOT / "instructions" / "README.md"],
)
def test_verwijst_naar_security_md(pad: Path) -> None:
    assert "SECURITY.md" in pad.read_text(), f"{pad} verwijst niet naar SECURITY.md"


def _bewijsbestanden() -> list[str]:
    """De repo-bestanden waar SECURITY.md naar linkt als bewijs voor een maatregel."""
    paden = []
    for doel in re.findall(r"\]\(([^)]+)\)", SECURITY.read_text()):
        if doel.startswith("http") or not (REPO_ROOT / doel).is_file():
            continue
        paden.append(doel)
    return sorted(set(paden))


def test_security_md_linkt_naar_bestaande_bestanden() -> None:
    assert _bewijsbestanden(), "SECURITY.md linkt naar geen enkel bestand in de repo"


@pytest.mark.parametrize("pad", _bewijsbestanden())
def test_bewijsbestand_heeft_een_eigenaar(pad: str) -> None:
    assert any(_raakt(patroon, pad) for patroon, _ in _regels()), (
        f"SECURITY.md noemt {pad} als bewijs, maar geen CODEOWNERS-patroon dekt het"
    )


@pytest.mark.parametrize(
    "pad",
    [
        "scripts/scan-secrets.py",
        "scripts/secret-scan-baseline.json",
        "workflow/secret-scan-allow.toml",
    ],
)
def test_secret_scan_en_zijn_allow_list_hebben_een_eigenaar(pad: str) -> None:
    assert (REPO_ROOT / pad).exists(), f"{pad} bestaat niet meer; werk deze lijst bij"
    assert any(_raakt(patroon, pad) for patroon, _ in _regels()), (
        f"{pad} bepaalt wat de secret-scan ziet, maar wordt door geen CODEOWNERS-patroon gedekt"
    )
