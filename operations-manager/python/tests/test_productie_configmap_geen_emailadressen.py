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
ACCESS_SECRET = OVERLAY / "operations-manager-access-secrets.yaml"
DECRYPT_SOPS = OVERLAY / "decrypt-sops.yaml"
KUSTOMIZATION = OVERLAY / "kustomization.yaml"
OVERLAY_STAGING = REPO_ROOT / "bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-staging"
CONFIGMAP_STAGING = OVERLAY_STAGING / "configmap.yaml"
SANDBOX_KUSTOMIZATION = (
    REPO_ROOT / "bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/kustomization.yaml"
)
BASE_DEPLOYMENT = REPO_ROOT / "bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml"
STARTUP = Path(__file__).resolve().parents[1] / "opi/core/startup.py"

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
        assert not gevonden, f"{sleutel} hoort in {ACCESS_SECRET.name}, niet in de configmap: {gevonden}"

    def test_bevat_geen_enkel_emailadres(self, env_regels: list[str]) -> None:
        gevonden = [r.strip() for r in env_regels if not r.strip().startswith("#") and EMAILADRES.search(r)]
        assert not gevonden, f"e-mailadressen in de leesbare productieconfiguratie: {gevonden}"


class TestDeOpstartcode:
    def test_zet_geen_hardgecodeerd_adres_in_de_allowlist(self) -> None:
        """De allowlist en de platformbeheerders komen uit de configuratie, niet uit de
        broncode. Zie rechten-plan-van-aanpak 4.4."""
        gevonden = EMAILADRES.findall(STARTUP.read_text())
        assert not gevonden, f"e-mailadressen in {STARTUP.name}: {gevonden}"


class TestHetAccessSecret:
    def test_is_versleuteld(self) -> None:
        inhoud = yaml.safe_load(ACCESS_SECRET.read_text())
        assert "sops" in inhoud, f"{ACCESS_SECRET.name} heeft geen sops-metadata"
        assert sorted(inhoud["data"]) == ["ADMIN_EMAILS", "ALLOWED_EMAILS"]
        for sleutel, waarde in inhoud["data"].items():
            assert waarde.startswith("ENC["), f"{sleutel} staat niet versleuteld"

    def test_wordt_door_de_generator_ontsleuteld(self) -> None:
        generator = yaml.safe_load(DECRYPT_SOPS.read_text())
        assert ACCESS_SECRET.name in generator["files"]


def _secret_ref_patches() -> list[dict]:
    """De JSON6902-patches op de Deployment die een secretRef toevoegen."""
    kustomization = yaml.safe_load(KUSTOMIZATION.read_text())
    return [
        patch
        for patch in kustomization["patches"]
        if patch.get("target", {}).get("kind") == "Deployment" and "patch" in patch
    ]


class TestDeEnvFromPatch:
    """envFrom heeft geen merge key: een strategic-merge-patch zou de twee bestaande
    secretRefs vervangen in plaats van aanvullen. Dit bewaakt dat het een JSON-patch
    met op "add" blijft, op de envFrom van de enige, vaste container.
    """

    def test_is_een_json_patch_die_toevoegt_niet_vervangt(self) -> None:
        patches = _secret_ref_patches()
        assert patches, "geen JSON6902-patch op de Deployment gevonden"

        operaties = yaml.safe_load(patches[0]["patch"])
        (operatie,) = operaties
        assert operatie["op"] == "add"
        assert operatie["path"] == "/spec/template/spec/containers/0/envFrom/-"
        assert operatie["value"]["secretRef"]["name"] == "operations-manager-access"

    def test_container_0_is_de_enige_container_operations_manager(self) -> None:
        """De patch wijst op index 0 zonder de container bij naam te kiezen. Komt er een
        tweede container voor operations-manager, dan raakt de patch de verkeerde."""
        deployment = yaml.safe_load(BASE_DEPLOYMENT.read_text())
        containers = deployment["spec"]["template"]["spec"]["containers"]
        assert len(containers) == 1
        assert containers[0]["name"] == "operations-manager"

    def test_de_sandbox_overlay_krijgt_deze_patch_niet(self) -> None:
        kustomization = yaml.safe_load(SANDBOX_KUSTOMIZATION.read_text())
        inhoud = str(kustomization)
        assert "operations-manager-access" not in inhoud
