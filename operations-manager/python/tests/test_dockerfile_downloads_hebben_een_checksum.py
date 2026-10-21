"""Elke download in een Dockerfile heeft een vaste versie en een literale checksum.

BIO2 8.19 en 5.21 vragen dat wat in een image komt vastligt en controleerbaar is. Een
vaste versie plus een `sha256sum -c` tegen een literaal in het Dockerfile maakt de bouw
herhaalbaar en laat een gewijzigd artefact omvallen. De vorm staat in images/mc/Dockerfile.

Een vaste versie die niemand ophoogt veroudert, dus hier staat er ook dat elke
`ARG <NAAM>_VERSION` zichtbaar is voor Renovate. Zie features/renovate.md.
"""

import re
import subprocess
from pathlib import Path

import pytest
from tests.test_image_layer_guard import _instructions

REPO_ROOT = Path(__file__).resolve().parents[3]

# De sleutel van een gesigneerde apt-repository: apt toetst daarna elk pakket tegen die
# sleutel, dus een checksum op de sleutel voegt niets toe aan wat er geïnstalleerd wordt.
UITZONDERINGEN: dict[tuple[str, str], str] = {
    (
        "operations-manager/Dockerfile",
        "https://pkgs.k8s.io/",
    ): "gesigneerde apt-repository, apt toetst de pakketten zelf",
}

HAALT_OP = re.compile(r"\b(curl|wget)\b")
URL = re.compile(r"https?://[^\s\"'\\|)]+")

ARG_VERSIE = re.compile(r"^ARG ([A-Za-z0-9_]+_VERSION)=")
ARG_VERWIJZING = re.compile(r"\$\{?([A-Za-z0-9_]+)\}?")

# Een versie die Renovate niet kan opzoeken, met de reden. Zie features/renovate.md.
ZONDER_ANNOTATIE: dict[tuple[str, str], str] = {
    (
        "operations-manager/Dockerfile",
        "ZAD_VERSION",
    ): "onze eigen versie, er is geen upstream om hem bij op te zoeken",
    (
        "images/jupyterhub-user/Dockerfile",
        "PYTHON_EXTENSION_VERSION",
    ): "Renovate heeft geen datasource voor de extensiemarktplaats",
    (
        "images/jupyterhub-user/Dockerfile",
        "BASEDPYRIGHT_VERSION",
    ): "Renovate heeft geen datasource voor de extensiemarktplaats",
}


def _dockerfiles() -> list[str]:
    uit = subprocess.run(
        ["git", "ls-files", "*Dockerfile", "**/Dockerfile"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    paden = sorted(set(uit.stdout.split()))
    assert paden, "git ls-files vond geen Dockerfile"
    return paden


def _bezwaren(pad: str, inhoud: str) -> list[str]:
    """Wat er aan de downloads in dit Dockerfile niet vastligt."""
    bezwaren: list[str] = []
    for _, instructie in _instructions(inhoud):
        if not instructie.upper().startswith("RUN ") or not HAALT_OP.search(instructie):
            continue
        urls = [
            url
            for url in URL.findall(instructie)
            if not any(bestand == pad and url.startswith(voorvoegsel) for bestand, voorvoegsel in UITZONDERINGEN)
        ]
        if not urls:
            continue
        if "sha256sum -c" not in instructie:
            bezwaren.append(f"{pad}: download zonder `sha256sum -c`: {urls[0]}")
        bezwaren.extend(f"{pad}: download zonder vaste versie: {url}" for url in urls if "latest" in url)
        if "$(curl" in instructie:
            bezwaren.append(f"{pad}: de versie komt uit een tweede download: {urls[0]}")
    return bezwaren


def _renovate_bezwaren(pad: str, inhoud: str) -> list[str]:
    """Welke `ARG <NAAM>_VERSION` in dit Dockerfile buiten het zicht van Renovate valt."""
    regels = inhoud.splitlines()
    in_from = {naam for regel in regels if regel.startswith("FROM ") for naam in ARG_VERWIJZING.findall(regel)}
    bezwaren: list[str] = []
    for nummer, regel in enumerate(regels):
        treffer = ARG_VERSIE.match(regel)
        if treffer is None:
            continue
        naam = treffer.group(1)
        if naam in in_from or (pad, naam) in ZONDER_ANNOTATIE:
            continue
        if nummer == 0 or not regels[nummer - 1].startswith("# renovate:"):
            bezwaren.append(f"{pad}:{nummer + 1}: {naam} zonder `# renovate:`-annotatie erboven")
    return bezwaren


@pytest.mark.parametrize("pad", _dockerfiles())
def test_elke_download_heeft_een_vaste_versie_en_een_checksum(pad: str) -> None:
    assert _bezwaren(pad, (REPO_ROOT / pad).read_text()) == []


@pytest.mark.parametrize("pad", _dockerfiles())
def test_elke_versie_is_zichtbaar_voor_renovate(pad: str) -> None:
    assert _renovate_bezwaren(pad, (REPO_ROOT / pad).read_text()) == []


def test_de_meting_ziet_een_ontbrekende_annotatie(tmp_path: Path) -> None:
    pad = "images/cmp-kustomize-sops/Dockerfile"
    origineel = (REPO_ROOT / pad).read_text()
    zonder = origineel.replace("# renovate: datasource=github-releases depName=getsops/sops\n", "")
    kopie = tmp_path / "Dockerfile"
    kopie.write_text(zonder)

    bezwaren = _renovate_bezwaren(pad, kopie.read_text())

    assert any("SOPS_VERSION zonder" in bezwaar for bezwaar in bezwaren), bezwaren


def test_een_versie_in_een_FROM_hoeft_geen_annotatie() -> None:
    inhoud = "ARG UV_VERSION=0.13.1\nFROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv\n"

    assert _renovate_bezwaren("test/Dockerfile", inhoud) == []


def test_de_meting_ziet_een_ontbrekende_checksum(tmp_path: Path) -> None:
    origineel = (REPO_ROOT / "images/mc/Dockerfile").read_text()
    zonder = "\n".join(regel for regel in origineel.splitlines() if "sha256sum -c" not in regel)
    kopie = tmp_path / "Dockerfile"
    kopie.write_text(zonder)

    bezwaren = _bezwaren("images/mc/Dockerfile", kopie.read_text())

    assert any("zonder `sha256sum -c`" in bezwaar for bezwaar in bezwaren), bezwaren


def test_de_meting_ziet_een_ontbrekende_vaste_versie() -> None:
    inhoud = (
        'RUN curl -fsSLo yq "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64" '
        '&& echo "abc  yq" | sha256sum -c -'
    )

    bezwaren = _bezwaren("test/Dockerfile", inhoud)

    assert any("zonder vaste versie" in bezwaar for bezwaar in bezwaren), bezwaren


def test_de_meting_ziet_een_versie_uit_een_tweede_download() -> None:
    inhoud = (
        'RUN curl -Lo kubectl "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)'
        '/bin/linux/amd64/kubectl" && echo "abc  kubectl" | sha256sum -c -'
    )

    bezwaren = _bezwaren("test/Dockerfile", inhoud)

    assert any("tweede download" in bezwaar for bezwaar in bezwaren), bezwaren


def test_de_uitzonderingslijst_ontslaat_alleen_het_genoemde_bestand() -> None:
    inhoud = 'RUN curl -fsSL "https://pkgs.k8s.io/core:/stable:/v1.32/deb/Release.key" | gpg --dearmor -o keyring.gpg'

    assert _bezwaren("operations-manager/Dockerfile", inhoud) == []
    bezwaren = _bezwaren("ander/Dockerfile", inhoud)
    assert any("zonder `sha256sum -c`" in bezwaar for bezwaar in bezwaren), bezwaren
