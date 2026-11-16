"""Persoonsgegevens (ALLOWED_EMAILS, ADMIN_EMAILS) gaan in secrets, niet in de configmap
en niet in de broncode.
Staging houdt ze uit deze publieke repo: ze staan in de private repo zad-staging-secrets.
Sandbox-adressen zijn uitgesloten.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY = REPO_ROOT / "bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production"
CONFIGMAP = OVERLAY / "configmap.yaml"
ENV_SECRET = OVERLAY / "operations-manager-env-secrets.yaml"
DECRYPT_SOPS = OVERLAY / "decrypt-sops.yaml"
OVERLAY_STAGING = REPO_ROOT / "bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-staging"
CONFIGMAP_STAGING = OVERLAY_STAGING / "configmap.yaml"
STARTUP = Path(__file__).resolve().parents[1] / "opi/runtime/startup/startup.py"

# Een e-mailadres in een waarde. De base64+age-blobs in deze configmap bevatten geen @,
# dus die halen deze vorm niet.
EMAILADRES = re.compile(r"[^\s=,\"']+@[^\s=,\"']+\.[a-z]{2,}")


@pytest.fixture(scope="module")
def env_regels() -> list[str]:
    """Regels van de .env in de configmap."""
    configmap = yaml.safe_load(CONFIGMAP.read_text())
    return configmap["data"][".env"].splitlines()


@pytest.fixture(scope="module")
def env_regels_staging() -> list[str]:
    """Regels van de .env in de staging-configmap."""
    configmap = yaml.safe_load(CONFIGMAP_STAGING.read_text())
    return configmap["data"][".env"].splitlines()


class TestDeStagingConfigmap:
    """Zelfde regel als productie: de toegangslijsten zijn persoonsgegevens en horen bij
    staging in zad-staging-secrets, uit deze repo."""

    @pytest.mark.parametrize("sleutel", ["ALLOWED_EMAILS", "ADMIN_EMAILS"])
    def test_zet_de_toegangslijsten_niet(self, env_regels_staging: list[str], sleutel: str) -> None:
        gevonden = [r for r in env_regels_staging if r.strip().startswith(f"{sleutel}=")]
        assert not gevonden, f"{sleutel} hoort in zad-staging-secrets, niet in de configmap: {gevonden}"

    def test_bevat_geen_enkel_emailadres(self, env_regels_staging: list[str]) -> None:
        gevonden = [r.strip() for r in env_regels_staging if not r.strip().startswith("#") and EMAILADRES.search(r)]
        assert not gevonden, f"e-mailadressen in de leesbare stagingconfiguratie: {gevonden}"


class TestDeProductieConfigmap:
    @pytest.mark.parametrize("sleutel", ["ALLOWED_EMAILS", "ADMIN_EMAILS"])
    def test_zet_de_toegangslijsten_niet(self, env_regels: list[str], sleutel: str) -> None:
        gevonden = [r for r in env_regels if r.strip().startswith(f"{sleutel}=")]
        assert not gevonden, f"{sleutel} hoort in {ENV_SECRET.name}, niet in de configmap: {gevonden}"

    def test_bevat_geen_enkel_emailadres(self, env_regels: list[str]) -> None:
        gevonden = [r.strip() for r in env_regels if not r.strip().startswith("#") and EMAILADRES.search(r)]
        assert not gevonden, f"e-mailadressen in de leesbare productieconfiguratie: {gevonden}"


class TestDeOpstartcode:
    def test_zet_geen_hardgecodeerd_adres_in_de_allowlist(self) -> None:
        """De allowlist en de platformbeheerders komen uit de configuratie, niet uit de
        broncode. Zie rechten-plan-van-aanpak 4.4."""
        gevonden = EMAILADRES.findall(STARTUP.read_text())
        assert not gevonden, f"e-mailadressen in {STARTUP.name}: {gevonden}"


class TestTheEnvSecret:
    """The access lists travel in the env secret, so they are always stored encrypted."""

    @pytest.mark.parametrize("key", ["ALLOWED_EMAILS", "ADMIN_EMAILS"])
    def test_holds_the_access_list_encrypted(self, key: str) -> None:
        content = yaml.safe_load(ENV_SECRET.read_text())
        assert "sops" in content, f"{ENV_SECRET.name} has no sops metadata"
        assert content["data"][key].startswith("ENC["), f"{key} is not encrypted"

    def test_is_decrypted_by_the_generator(self) -> None:
        generator = yaml.safe_load(DECRYPT_SOPS.read_text())
        assert ENV_SECRET.name in generator["files"]
