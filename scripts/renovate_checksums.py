#!/usr/bin/env python3
"""Zet de literale sha256-sommen in de Dockerfiles gelijk aan de versies die ernaast staan.

Renovate hoogt wel een `ARG <TOOL>_VERSION` op, maar niet de `ARG <TOOL>_SHA256_<ARCH>`
ernaast, en dan faalt de bouw op `sha256sum -c`. Renovate draait dit als postUpgradeTasks,
zodat de nieuwe sommen in dezelfde PR staan. Met de hand ophogen gaat net zo, zie
images/README.md.

Het raakt alleen de sommen van een versie die in deze werkboom wijzigt. Een som opnieuw
pinnen zonder versiewijziging zou de grendel van RC-294 ontwapenen: dan valt een upstream
dat zijn artefact onder een bestaande tag vervangt niet meer op `sha256sum -c`. Met
`--alles` rekent het alles door, voor wie met de hand wil nalopen.

Alleen de standaardbibliotheek: op de Renovate-runner draait dit als `python3`.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TIMEOUT = 120


@dataclass(frozen=True)
class Release:
    """Waar het artefact van een release staat, en waar de som ervan vandaan komt.

    In de URL's staat `{versie}` voor de waarde van de ARG, `{kaal}` voor die waarde zonder
    leidende `v`, en `{arch}` voor de architectuur zoals de bestandsnaam hem noemt.
    """

    artefact: str
    archen: dict[str, str] = field(default_factory=lambda: {"AMD64": "amd64", "ARM64": "arm64"})
    checksums: str | None = None
    hash_orde: str | None = None


GITHUB = "https://github.com"

# Per ARG-voorvoegsel. `checksums=None` betekent: het project publiceert geen som bij de
# release, dus het script rekent hem uit over het artefact en zegt dat in zijn uitvoer.
RELEASES: dict[str, Release] = {
    "KUBECTL": Release(
        artefact="https://dl.k8s.io/release/{versie}/bin/linux/{arch}/kubectl",
        checksums="https://dl.k8s.io/release/{versie}/bin/linux/{arch}/kubectl.sha256",
    ),
    "KUSTOMIZE": Release(
        artefact=f"{GITHUB}/kubernetes-sigs/kustomize/releases/download/kustomize/v{{kaal}}/kustomize_v{{kaal}}_linux_{{arch}}.tar.gz",
        checksums=f"{GITHUB}/kubernetes-sigs/kustomize/releases/download/kustomize/v{{kaal}}/checksums.txt",
    ),
    "SOPS": Release(
        artefact=f"{GITHUB}/getsops/sops/releases/download/{{versie}}/sops-{{versie}}.linux.{{arch}}",
        checksums=f"{GITHUB}/getsops/sops/releases/download/{{versie}}/sops-{{versie}}.checksums.txt",
    ),
    # yq publiceert geen sha256sum-bestand maar een tabel met 31 hashkolommen, met de volgorde
    # van die kolommen in een bestand ernaast.
    "YQ": Release(
        artefact=f"{GITHUB}/mikefarah/yq/releases/download/{{versie}}/yq_linux_{{arch}}",
        checksums=f"{GITHUB}/mikefarah/yq/releases/download/{{versie}}/checksums",
        hash_orde=f"{GITHUB}/mikefarah/yq/releases/download/{{versie}}/checksums_hashes_order",
    ),
    "HELM": Release(
        artefact="https://get.helm.sh/helm-{versie}-linux-{arch}.tar.gz",
        checksums="https://get.helm.sh/helm-{versie}-linux-{arch}.tar.gz.sha256sum",
    ),
    "HELMFILE": Release(
        artefact=f"{GITHUB}/helmfile/helmfile/releases/download/v{{kaal}}/helmfile_{{kaal}}_linux_{{arch}}.tar.gz",
        checksums=f"{GITHUB}/helmfile/helmfile/releases/download/v{{kaal}}/helmfile_{{kaal}}_checksums.txt",
    ),
    "HELM_DIFF": Release(
        artefact=f"{GITHUB}/databus23/helm-diff/releases/download/v{{kaal}}/helm-diff-linux-{{arch}}.tgz",
    ),
    "KSOPS": Release(
        artefact=f"{GITHUB}/viaduct-ai/kustomize-sops/releases/download/v{{kaal}}/ksops_{{kaal}}_Linux_{{arch}}.tar.gz",
        archen={"AMD64": "x86_64", "ARM64": "arm64"},
        checksums=f"{GITHUB}/viaduct-ai/kustomize-sops/releases/download/v{{kaal}}/checksums.txt",
    ),
    "KOPIA": Release(
        artefact=f"{GITHUB}/kopia/kopia/releases/download/v{{kaal}}/kopia-{{kaal}}-linux-{{arch}}.tar.gz",
        archen={"AMD64": "x64", "ARM64": "arm64", "X64": "x64"},
        checksums=f"{GITHUB}/kopia/kopia/releases/download/v{{kaal}}/checksums.txt",
    ),
    "CHISEL": Release(
        artefact=f"{GITHUB}/jpillora/chisel/releases/download/v{{kaal}}/chisel_{{kaal}}_linux_{{arch}}.gz",
        checksums=f"{GITHUB}/jpillora/chisel/releases/download/v{{kaal}}/chisel_{{kaal}}_checksums.txt",
    ),
    "MC": Release(
        artefact=f"{GITHUB}/minio/mc/releases/download/{{versie}}/mc.linux-{{arch}}.{{versie}}",
        checksums=f"{GITHUB}/minio/mc/releases/download/{{versie}}/mc.linux-{{arch}}.{{versie}}.sha256sum",
    ),
    "CODE_SERVER": Release(
        artefact=f"{GITHUB}/coder/code-server/releases/download/v{{kaal}}/code-server_{{kaal}}_{{arch}}.deb",
    ),
}

ARG_VERSIE = re.compile(r"^ARG ([A-Z0-9_]+)_VERSION=(\S+)$", re.MULTILINE)
ARG_SOM = re.compile(r"^ARG ([A-Z0-9_]+)_SHA256_([A-Z0-9]+)=([0-9a-f]{64})$", re.MULTILINE)
LITERALE_SOM = re.compile(r'echo "([0-9a-f]{64}) {2}(\S+)"')
URL_IN_INSTRUCTIE = re.compile(r'https?://[^\s"\'\\|)]+')


class Onvindbaar(Exception):
    """Het Dockerfile noemt iets waar dit script geen bron voor kent."""


def toegevoegde_regels(pad: Path) -> list[str]:
    """De regels die deze werkboom aan het bestand toevoegt ten opzichte van HEAD.

    Renovate schrijft de opgehoogde versies in de werkboom voordat het de postUpgradeTasks
    draait en pas daarna commit, dus hier staat wat deze branch aan versies wijzigt.
    """
    uit = subprocess.run(  # noqa: S603
        ["git", "diff", "-U0", "HEAD", "--", str(pad)],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [regel[1:] for regel in uit.stdout.splitlines() if regel.startswith("+") and not regel.startswith("+++")]


def _haal(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as antwoord:  # noqa: S310
        return antwoord.read()


def som_uit_sha256sum(tekst: str, bestandsnaam: str) -> str:
    """De som uit een `<som>  <naam>`-bestand, of uit een bestand met alleen de som.

    Dekt zowel een goreleaser-`checksums.txt` met alle artefacten als een `.sha256sum` naast
    een enkel artefact. Staat er maar een regel, dan telt de naam niet mee: minio noemt het
    artefact daar anders dan de URL.
    """
    regels = [regel.split() for regel in tekst.splitlines() if regel.strip()]
    if len(regels) == 1:
        return regels[0][0]
    for velden in regels:
        if len(velden) >= 2 and velden[1].lstrip("*") == bestandsnaam:
            return velden[0]
    raise Onvindbaar(f"geen regel voor {bestandsnaam} in het checksumbestand")


def som_uit_hash_tabel(tekst: str, orde: str, bestandsnaam: str) -> str:
    """De SHA-256-kolom uit de hashtabel van yq, waarvan de kolomvolgorde ernaast staat."""
    kolommen = orde.split()
    if "SHA-256" not in kolommen:
        raise Onvindbaar("de kolomvolgorde noemt geen SHA-256")
    kolom = kolommen.index("SHA-256") + 1
    for regel in tekst.splitlines():
        velden = regel.split()
        if velden and velden[0] == bestandsnaam and len(velden) > kolom:
            return velden[kolom]
    raise Onvindbaar(f"geen regel voor {bestandsnaam} in de hashtabel")


def som_van_release(release: Release, versie: str, arch: str) -> tuple[str, str]:
    """De sha256 voor een architectuur, met waar hij vandaan komt."""
    sleutels = {"versie": versie, "kaal": versie.lstrip("v"), "arch": arch}
    artefact = release.artefact.format(**sleutels)
    bestandsnaam = artefact.rsplit("/", 1)[-1]
    if release.checksums is None:
        return hashlib.sha256(_haal(artefact)).hexdigest(), f"berekend over {artefact}"
    checksums = release.checksums.format(**sleutels)
    tekst = _haal(checksums).decode()
    if release.hash_orde is not None:
        orde = _haal(release.hash_orde.format(**sleutels)).decode()
        return som_uit_hash_tabel(tekst, orde, bestandsnaam), checksums
    return som_uit_sha256sum(tekst, bestandsnaam), checksums


def instructies(inhoud: str) -> list[str]:
    """De instructies van het Dockerfile, met voortgezette regels samengevoegd."""
    uit: list[str] = []
    lopend = ""
    for regel in inhoud.splitlines():
        lopend += regel
        if regel.rstrip().endswith("\\"):
            lopend = lopend.rstrip()[:-1]
            continue
        uit.append(lopend)
        lopend = ""
    if lopend:
        uit.append(lopend)
    return uit


def _arg_sommen(inhoud: str, toegevoegd: list[str] | None) -> list[tuple[str, str]]:
    """Wat er aan `ARG <TOOL>_SHA256_<ARCH>` moet veranderen, als (oude regel, nieuwe regel)."""
    versies = dict(ARG_VERSIE.findall(inhoud))
    wijzigingen: list[tuple[str, str]] = []
    for tool, achtervoegsel, oud in ARG_SOM.findall(inhoud):
        if tool not in RELEASES:
            raise Onvindbaar(f"geen release bekend voor ARG {tool}_SHA256_{achtervoegsel}")
        if tool not in versies:
            raise Onvindbaar(f"ARG {tool}_SHA256_{achtervoegsel} zonder ARG {tool}_VERSION")
        release = RELEASES[tool]
        if achtervoegsel not in release.archen:
            raise Onvindbaar(f"onbekende architectuur in ARG {tool}_SHA256_{achtervoegsel}")
        if toegevoegd is not None and not any(r.startswith(f"ARG {tool}_VERSION=") for r in toegevoegd):
            continue
        nieuw, bron = som_van_release(release, versies[tool], release.archen[achtervoegsel])
        regel = f"ARG {tool}_SHA256_{achtervoegsel}="
        print(f"  {tool}_SHA256_{achtervoegsel} {versies[tool]}: {nieuw} ({bron})")
        if nieuw != oud:
            wijzigingen.append((f"{regel}{oud}", f"{regel}{nieuw}"))
    return wijzigingen


def _literale_sommen(inhoud: str, toegevoegd: list[str] | None) -> list[tuple[str, str]]:
    """Wat er aan een uitgeschreven som in een RUN-instructie moet veranderen.

    Die vorm komt voor waar de versie bewust geen ARG is, en er dus ook geen ARG is om de
    som in te zetten. De instructie moet dan precies een URL noemen. Sinds RC-424 gebruikt
    geen Dockerfile in deze repo hem nog; de vorm blijft omdat de volgende die hem nodig
    heeft er niets voor hoeft te bouwen.
    """
    wijzigingen: list[tuple[str, str]] = []
    for instructie in instructies(inhoud):
        treffers = LITERALE_SOM.findall(instructie)
        if not treffers:
            continue
        urls = URL_IN_INSTRUCTIE.findall(instructie)
        if len(treffers) != 1 or len(urls) != 1:
            raise Onvindbaar(f"een uitgeschreven som hoort bij precies een URL: {instructie[:80]}")
        (oud, naam), url = treffers[0], urls[0]
        if toegevoegd is not None and not any(url in r for r in toegevoegd):
            continue
        nieuw = hashlib.sha256(_haal(url)).hexdigest()
        print(f"  {naam}: {nieuw} (berekend over {url})")
        if nieuw != oud:
            wijzigingen.append((f'echo "{oud}  {naam}"', f'echo "{nieuw}  {naam}"'))
    return wijzigingen


def werk_bij(pad: Path, toegevoegd: list[str] | None = None) -> bool:
    """Zet de sommen in dit Dockerfile gelijk aan zijn versies. Geeft terug of er iets wijzigde.

    `toegevoegd` zijn de regels die deze werkboom toevoegt: alleen een som waarvan de versie
    daarin staat wordt aangeraakt. `None` rekent alles door.
    """
    inhoud = pad.read_text()
    print(f"{pad}:")
    nieuwe_inhoud = inhoud
    for oud, nieuw in _arg_sommen(inhoud, toegevoegd) + _literale_sommen(inhoud, toegevoegd):
        nieuwe_inhoud = nieuwe_inhoud.replace(oud, nieuw)
    if nieuwe_inhoud == inhoud:
        return False
    pad.write_text(nieuwe_inhoud)
    return True


def dockerfiles() -> list[Path]:
    uit = subprocess.run(
        ["git", "ls-files", "*Dockerfile", "**/Dockerfile"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / pad for pad in sorted(set(uit.stdout.split()))]


def main(argumenten: list[str]) -> int:
    alles = "--alles" in argumenten
    paden = [Path(a).resolve() for a in argumenten if a != "--alles"] or dockerfiles()
    gewijzigd = [pad for pad in paden if werk_bij(pad, None if alles else toegevoegde_regels(pad))]
    print(f"gewijzigd: {len(gewijzigd)} van {len(paden)}")
    for pad in gewijzigd:
        print(f"  {pad}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
