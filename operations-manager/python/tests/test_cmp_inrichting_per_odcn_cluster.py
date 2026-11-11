"""RC-359: elk ODCN-cluster krijgt dezelfde CMP-inrichting, uit een gedeelde bron.

Toen odcn-staging erbij kwam was zijn overlay een kopie van productie van voor RC-303, en
die kopie liep stil uit elkaar: beide sidecars draaiden op `:latest` in plaats van op de
digest uit de rcr-spiegel, ook de sidecar die de platformsleutel gemount heeft. Sinds
RC-359 staat de gedeelde inrichting in de component `cmp-odcn` en houdt de overlay alleen
het UID van zijn eigen namespace over.

De overlays worden echt gerenderd: een toets op de patches zelf zou niet zien of ze
aankomen op de ArgoCD-CR, en bij welke index.
"""

from __future__ import annotations

import copy
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
KUSTOMIZE_ROOT = REPO_ROOT / "bootstrap/rig-system/kustomize"
OVERLAYS = KUSTOMIZE_ROOT / "overlays"

#: Het UID uit de SCC-annotatie van de namespace, per cluster.
UIDS = {"odcn-production": 1001620000, "odcn-staging": 1003110000}

#: The CPU limit per sidecar, per cluster: staging's tenant quota has no room for two.
CPU_LIMITS = {"odcn-production": "2", "odcn-staging": "1"}

#: De registry die ODCN wel kan bereiken. De admission-herschrijving van ODCN zet elk ander
#: pad hierheen, dus zonder dit pad in git blijft de app voor altijd OutOfSync.
RCR = "rcr.rijksapps.nl/ghcr-rig/"


def _odcn_overlays() -> list[str]:
    gevonden = [pad.name for pad in sorted(OVERLAYS.iterdir()) if pad.name.startswith("odcn-")]
    assert gevonden, "geen odcn-overlay gevonden"
    return gevonden


ODCN = _odcn_overlays()


def _zonder_ksops(kustomization: Path) -> None:
    """De OPI-overlay en haar ksops-generator eruit: die vraagt de clustersleutel.

    Beide overlays krijgen dezelfde behandeling, dus de vergelijking blijft eerlijk. Wat
    deze toets meet (de ArgoCD-CR) zit er niet in.
    """
    inhoud = yaml.safe_load(kustomization.read_text())
    inhoud.pop("generators", None)
    inhoud["resources"] = [pad for pad in inhoud["resources"] if not pad.startswith("../../operations-manager/")]
    kustomization.write_text(yaml.safe_dump(inhoud))


def _argocd_cr(overlay: str, werkmap: Path) -> dict[str, Any]:
    """De ArgoCD-CR uit de gerenderde overlay, gebouwd in een kopie van de kustomize-boom."""
    boom = werkmap / "kustomize"
    if not boom.exists():
        shutil.copytree(KUSTOMIZE_ROOT, boom)
    _zonder_ksops(boom / "overlays" / overlay / "kustomization.yaml")
    proc = subprocess.run(
        # De component sops-plugin leest boven zichzelf, vandaar de load-restrictor.
        ["kustomize", "build", "--load-restrictor", "LoadRestrictionsNone", f"overlays/{overlay}"],
        cwd=boom,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return next(doc for doc in yaml.safe_load_all(proc.stdout) if doc and doc.get("kind") == "ArgoCD")


@pytest.fixture(scope="module")
def repo_specs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """Per ODCN-cluster de gerenderde ``spec.repo`` van zijn ArgoCD-CR."""
    if shutil.which("kustomize") is None:
        pytest.skip("kustomize ontbreekt")
    werkmap = tmp_path_factory.mktemp("render")
    return {overlay: _argocd_cr(overlay, werkmap)["spec"]["repo"] for overlay in ODCN}


def _zonder_uid(repo: dict[str, Any]) -> dict[str, Any]:
    kaal = copy.deepcopy(repo)
    for sidecar in kaal["sidecarContainers"]:
        sidecar["securityContext"]["runAsUser"] = "<UID per namespace>"
        sidecar["resources"]["limits"]["cpu"] = "<CPU limit per cluster>"
    return kaal


class TestDeTweeOdcnClustersRenderenGelijk:
    def test_de_wandeling_vindt_beide_clusters(self) -> None:
        """Een lege lijst zou elke toets hieronder over niets laten slagen."""
        assert ODCN == ["odcn-production", "odcn-staging"]

    def test_de_repo_spec_is_gelijk_op_het_uid_en_de_cpu_limiet_na(self, repo_specs: dict[str, dict[str, Any]]) -> None:
        """Het verschil dat deze taak wegnam: staging stond op een overschrijfbare tag.

        De namespace valt buiten deze vergelijking omdat hij niet in ``spec.repo`` staat,
        maar op de metadata van de CR.
        """
        kaal = {overlay: _zonder_uid(repo) for overlay, repo in repo_specs.items()}

        assert kaal["odcn-staging"] == kaal["odcn-production"]

    @pytest.mark.parametrize("overlay", ODCN)
    def test_both_sidecars_have_the_cpu_limit_of_their_cluster(
        self, overlay: str, repo_specs: dict[str, dict[str, Any]]
    ) -> None:
        limits = {c["name"]: c["resources"]["limits"]["cpu"] for c in repo_specs[overlay]["sidecarContainers"]}

        assert limits == {"cmp-server": CPU_LIMITS[overlay], "cmp-platform": CPU_LIMITS[overlay]}

    @pytest.mark.parametrize("overlay", ODCN)
    def test_beide_sidecars_draaien_op_het_uid_van_de_eigen_namespace(
        self, overlay: str, repo_specs: dict[str, dict[str, Any]]
    ) -> None:
        """Niet op de 999 van de component: OpenShift laat alleen zijn SCC-bereik toe."""
        uids = {c["name"]: c["securityContext"]["runAsUser"] for c in repo_specs[overlay]["sidecarContainers"]}

        assert uids == {"cmp-server": UIDS[overlay], "cmp-platform": UIDS[overlay]}

    @pytest.mark.parametrize("overlay", ODCN)
    def test_beide_sidecars_staan_op_een_digest_uit_de_rcr_spiegel(
        self, overlay: str, repo_specs: dict[str, dict[str, Any]]
    ) -> None:
        """Een van de twee heeft de platformsleutel gemount, dus een tag is daar te weinig."""
        images = {c["name"]: c["image"] for c in repo_specs[overlay]["sidecarContainers"]}

        assert len(set(images.values())) == 1, images
        for naam, image in images.items():
            assert image.startswith(RCR), f"{overlay}/{naam} haalt zijn image niet uit de rcr-spiegel: {image}"
            assert "@sha256:" in image, f"{overlay}/{naam} staat niet op een digest: {image}"


class TestDeGedeeldeBron:
    def test_beide_overlays_nemen_de_component_cmp_odcn_op(self) -> None:
        """Zodat een volgend cluster de inrichting heel krijgt in plaats van half."""
        for overlay in ODCN:
            kustomization = yaml.safe_load((OVERLAYS / overlay / "kustomization.yaml").read_text())
            assert kustomization["components"] == ["../../components/cmp-odcn"], overlay

    def test_de_component_neemt_de_sops_plugin_zelf_op(self) -> None:
        component = yaml.safe_load((KUSTOMIZE_ROOT / "components/cmp-odcn/kustomization.yaml").read_text())
        assert component["components"] == ["../sops-plugin"]

    def test_geen_overlay_pint_het_image_nog_een_tweede_keer(self) -> None:
        """Een tweede pin naast die van de component is precies hoe staging achterop raakte."""
        for overlay in ODCN:
            tekst = (OVERLAYS / overlay / "kustomization.yaml").read_text()
            assert "rig-cmp-argo-kustomize-sops" not in tekst, overlay


class TestDeGrensTussenDeSidecars:
    """De platform-sidecar heeft de platformsleutel gemount en hoort geen weg naar de
    API-server te hebben. De mounts staan in de component sops-plugin en worden daar
    getoetst (``test_cmp_sleutel_per_app.py``); de env komt sinds RC-359 uit ``cmp-odcn``,
    en dat is de helft die hier gemeten wordt.
    """

    @pytest.mark.parametrize("overlay", ODCN)
    def test_alleen_de_tenant_sidecar_praat_met_de_api_server(
        self, overlay: str, repo_specs: dict[str, dict[str, Any]]
    ) -> None:
        env = {c["name"]: {e["name"] for e in c.get("env", [])} for c in repo_specs[overlay]["sidecarContainers"]}

        assert "KUBERNETES_SERVICE_HOST" in env["cmp-server"]
        assert "KUBERNETES_SERVICE_HOST" not in env["cmp-platform"]
