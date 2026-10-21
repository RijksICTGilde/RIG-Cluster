"""Tests voor scripts/renovate_checksums.py.

Renovate hoogt een `ARG <TOOL>_VERSION` op maar niet de `ARG <TOOL>_SHA256_<ARCH>` ernaast,
en dan faalt de bouw op `sha256sum -c`. Dit script schrijft die sommen mee. Wat hier wordt
gemeten is het lezen van de vier vormen waarin een project zijn som publiceert, en het
terugschrijven in het Dockerfile. Het net ophalen zit in een fixture: deze tests gaan niet
het internet op.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import renovate_checksums as tool  # noqa: E402

SOM_AMD64 = "a" * 64
SOM_ARM64 = "b" * 64

GORELEASER = f"""{SOM_AMD64}  kopia-0.23.1-linux-x64.tar.gz
{SOM_ARM64}  kopia-0.23.1-linux-arm64.tar.gz
"""

YQ_ORDE = "CRC32\nMD5\nSHA-1\nSHA-256\nSHA-512\n"
YQ_TABEL = f"yq_linux_amd64  1167c4b7  731c8ac2  9dc0ad19  {SOM_AMD64}  deadbeef\n"


def test_goreleaser_checksums_kiest_de_regel_van_het_artefact() -> None:
    assert tool.som_uit_sha256sum(GORELEASER, "kopia-0.23.1-linux-arm64.tar.gz") == SOM_ARM64


def test_een_ster_voor_de_bestandsnaam_telt_niet_mee() -> None:
    tekst = f"{SOM_AMD64} *helmfile_1.7.1_linux_amd64.tar.gz\n{SOM_ARM64} *helmfile_1.7.1_linux_arm64.tar.gz\n"

    assert tool.som_uit_sha256sum(tekst, "helmfile_1.7.1_linux_amd64.tar.gz") == SOM_AMD64


def test_een_sha256sum_naast_een_artefact_mag_een_andere_naam_noemen() -> None:
    """minio noemt het bestand in zijn .sha256sum anders dan in de download-URL."""
    tekst = f"{SOM_AMD64} mc.RELEASE.2025-08-13T08-35-41Z\n"

    assert tool.som_uit_sha256sum(tekst, "mc.linux-amd64.RELEASE.2025-08-13T08-35-41Z") == SOM_AMD64


def test_een_bestand_met_alleen_de_som() -> None:
    assert tool.som_uit_sha256sum(f"{SOM_AMD64}\n", "kubectl") == SOM_AMD64


def test_een_onbekend_artefact_in_het_checksumbestand_is_een_fout() -> None:
    with pytest.raises(tool.Onvindbaar):
        tool.som_uit_sha256sum(GORELEASER, "kopia-0.23.1-linux-riscv.tar.gz")


def test_de_hashtabel_van_yq_volgt_de_kolomvolgorde_ernaast() -> None:
    assert tool.som_uit_hash_tabel(YQ_TABEL, YQ_ORDE, "yq_linux_amd64") == SOM_AMD64


def test_zonder_checksumbestand_rekent_het_script_over_het_artefact(capsys: pytest.CaptureFixture[str]) -> None:
    artefact = b"een .deb dat code-server niet van een som voorziet"
    release = tool.Release(artefact="https://example/code-server_{kaal}_{arch}.deb")

    with patch.object(tool, "_haal", return_value=artefact):
        som, bron = tool.som_van_release(release, "4.141.0", "amd64")

    assert som == hashlib.sha256(artefact).hexdigest()
    assert bron.startswith("berekend over ")


def _dockerfile(tmp_path: Path, versie: str, som: str) -> Path:
    pad = tmp_path / "Dockerfile"
    pad.write_text(
        "FROM alpine:3.24\n"
        "# renovate: datasource=github-releases depName=kopia/kopia extractVersion=^v(?<version>.+)$\n"
        f"ARG KOPIA_VERSION={versie}\n"
        f"ARG KOPIA_SHA256_X64={som}\n"
        'RUN wget -q "https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/'
        'kopia-${KOPIA_VERSION}-linux-x64.tar.gz" -O /tmp/kopia.tar.gz \\\n'
        '    && echo "${KOPIA_SHA256_X64}  /tmp/kopia.tar.gz" | sha256sum -c -\n'
    )
    return pad


def test_een_nieuwe_versie_krijgt_de_som_van_die_versie(tmp_path: Path) -> None:
    pad = _dockerfile(tmp_path, "0.23.1", "c" * 64)

    with patch.object(tool, "_haal", return_value=GORELEASER.encode()):
        gewijzigd = tool.werk_bij(pad)

    assert gewijzigd
    assert f"ARG KOPIA_SHA256_X64={SOM_AMD64}" in pad.read_text()
    assert "ARG KOPIA_VERSION=0.23.1" in pad.read_text()


def test_een_som_die_al_klopt_laat_het_bestand_met_rust(tmp_path: Path) -> None:
    pad = _dockerfile(tmp_path, "0.23.1", SOM_AMD64)
    voor = pad.read_text()

    with patch.object(tool, "_haal", return_value=GORELEASER.encode()):
        gewijzigd = tool.werk_bij(pad)

    assert not gewijzigd
    assert pad.read_text() == voor


def _dockerfile_met_uitgeschreven_som(tmp_path: Path) -> tuple[Path, str]:
    url = "https://github.com/MinBZK/keycloak-theme/releases/download/v1.4.3/thema.jar"
    pad = tmp_path / "Dockerfile"
    pad.write_text(
        "FROM maven:3.10-eclipse-temurin-21\n"
        "RUN curl -fsSL -o thema.jar \\\n"
        f'      "{url}" \\\n'
        f' && echo "{"d" * 64}  thema.jar" \\\n'
        "    | sha256sum -c -\n"
    )
    return pad, url


def test_een_uitgeschreven_som_zonder_arg_wordt_ook_bijgewerkt(tmp_path: Path) -> None:
    """De vorm van images/keycloak-zad: de versie staat in de URL en niet als ARG."""
    jar = b"de jar van het thema"
    pad, _ = _dockerfile_met_uitgeschreven_som(tmp_path)

    with patch.object(tool, "_haal", return_value=jar):
        assert tool.werk_bij(pad)

    assert f'echo "{hashlib.sha256(jar).hexdigest()}  thema.jar"' in pad.read_text()


def test_een_som_zonder_bekende_release_is_een_fout(tmp_path: Path) -> None:
    pad = tmp_path / "Dockerfile"
    pad.write_text("ARG ONBEKEND_VERSION=1.0.0\nARG ONBEKEND_SHA256_AMD64=" + "e" * 64 + "\n")

    with pytest.raises(tool.Onvindbaar, match="ONBEKEND"):
        tool.werk_bij(pad)


def test_elke_som_in_de_repo_heeft_een_release_in_de_tabel() -> None:
    """Een nieuw gereedschap zonder tabelregel zou pas bij de eerste Renovate-PR omvallen."""
    ontbreekt: list[str] = []
    for pad in tool.dockerfiles():
        for gereedschap, achtervoegsel, _ in tool.ARG_SOM.findall(pad.read_text()):
            release = tool.RELEASES.get(gereedschap)
            if release is None or achtervoegsel not in release.archen:
                ontbreekt.append(f"{pad.name}: {gereedschap}_SHA256_{achtervoegsel}")

    assert ontbreekt == []


def test_een_som_van_een_ongewijzigde_versie_blijft_staan(tmp_path: Path) -> None:
    """De grendel van RC-294: zonder versiewijziging mag een som niet opnieuw gepind worden."""
    pad = _dockerfile(tmp_path, "0.23.1", "c" * 64)
    voor = pad.read_text()

    with patch.object(tool, "_haal", side_effect=AssertionError("mag niets ophalen")):
        gewijzigd = tool.werk_bij(pad, toegevoegd=[])

    assert not gewijzigd
    assert pad.read_text() == voor


def test_een_gewijzigde_versie_krijgt_wel_een_nieuwe_som(tmp_path: Path) -> None:
    pad = _dockerfile(tmp_path, "0.23.1", "c" * 64)

    with patch.object(tool, "_haal", return_value=GORELEASER.encode()):
        gewijzigd = tool.werk_bij(pad, toegevoegd=["ARG KOPIA_VERSION=0.23.1"])

    assert gewijzigd
    assert f"ARG KOPIA_SHA256_X64={SOM_AMD64}" in pad.read_text()


def test_een_uitgeschreven_som_blijft_staan_zolang_de_url_niet_wijzigt(tmp_path: Path) -> None:
    pad, _ = _dockerfile_met_uitgeschreven_som(tmp_path)
    voor = pad.read_text()

    with patch.object(tool, "_haal", side_effect=AssertionError("mag niets ophalen")):
        assert not tool.werk_bij(pad, toegevoegd=["ARG IETS_ANDERS=1.0.0"])

    assert pad.read_text() == voor


def test_een_uitgeschreven_som_volgt_een_gewijzigde_url(tmp_path: Path) -> None:
    pad, url = _dockerfile_met_uitgeschreven_som(tmp_path)
    jar = b"de jar van het thema"

    with patch.object(tool, "_haal", return_value=jar):
        assert tool.werk_bij(pad, toegevoegd=[f'      "{url}" '])

    assert f'echo "{hashlib.sha256(jar).hexdigest()}  thema.jar"' in pad.read_text()


def test_toegevoegde_regels_leest_de_werkboom(tmp_path: Path) -> None:
    """Renovate schrijft de ophoging in de werkboom en commit pas na de postUpgradeTasks."""
    pad = _dockerfile(tmp_path, "0.23.0", "c" * 64)
    for argumenten in (
        ["init", "-q"],
        ["add", "Dockerfile"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "basis"],
    ):
        subprocess.run(["git", *argumenten], cwd=tmp_path, check=True, capture_output=True)
    pad.write_text(pad.read_text().replace("KOPIA_VERSION=0.23.0", "KOPIA_VERSION=0.23.1"))

    with patch.object(tool, "REPO_ROOT", tmp_path):
        toegevoegd = tool.toegevoegde_regels(pad)

    assert toegevoegd == ["ARG KOPIA_VERSION=0.23.1"]
