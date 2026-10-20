"""Elke download in een Dockerfile heeft een vaste versie en een literale checksum.

BIO2 8.19 en 5.21 vragen dat wat in een image komt vastligt en controleerbaar is. Een
vaste versie plus een `sha256sum -c` tegen een literaal in het Dockerfile maakt de bouw
herhaalbaar en laat een gewijzigd artefact omvallen. De vorm staat in images/mc/Dockerfile.
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


@pytest.mark.parametrize("pad", _dockerfiles())
def test_elke_download_heeft_een_vaste_versie_en_een_checksum(pad: str) -> None:
    assert _bezwaren(pad, (REPO_ROOT / pad).read_text()) == []


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
