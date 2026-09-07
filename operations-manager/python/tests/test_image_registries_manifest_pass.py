"""De manifestpas: elke image in een podspec langs dezelfde regels.

Niet elke image loopt door de componentlus -- een sidecar staat als vaste waarde in zijn
sjabloon, en de backup-, db-console- en jobpods zijn kale ``Pod``s die los worden
toegepast. Deze pas dekt die plekken, met dezelfde regels en dezelfde ``resolve_image()``,
zodat er geen tweede tabel is die kan gaan afwijken.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from opi.services.catalog.image_registries.manifest_pass import (
    apply_rules,
    apply_rules_to_directory,
    apply_rules_to_document,
    pod_spec_of,
)
from opi.services.catalog.image_registries.resolution import cluster_rules
from opi.services.catalog.image_registries.rules import RegistryRule

ODCN = "odcn-production"
SANDBOX = "sandboxed-local"

GHCR = RegistryRule(match="ghcr.io", to="rcr.rijksapps.nl/ghcr-rig", secret="ghcr-rig-robot-pull-secret")
QUAY = RegistryRule(match="quay.io", to="rcr.rijksapps.nl/quay-rig", secret="quay-rig-robot-pull-secret")


def _deployment(*images: str) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web"},
        "spec": {"template": {"spec": {"containers": [{"name": f"c{i}", "image": im} for i, im in enumerate(images)]}}},
    }


class TestWaarDePodspecZit:
    @pytest.mark.parametrize(
        ("manifest", "gevonden"),
        [
            ({"kind": "Pod", "spec": {"containers": []}}, True),
            ({"kind": "Deployment", "spec": {"template": {"spec": {"containers": []}}}}, True),
            ({"kind": "StatefulSet", "spec": {"template": {"spec": {"containers": []}}}}, True),
            ({"kind": "CronJob", "spec": {"jobTemplate": {"spec": {"template": {"spec": {}}}}}}, True),
            ({"kind": "Service", "spec": {"ports": []}}, False),
            ({"kind": "Secret"}, False),
        ],
    )
    def test_per_kind(self, manifest: dict[str, Any], gevonden: bool) -> None:
        assert (pod_spec_of(manifest) is not None) is gevonden


class TestDePasZelf:
    def test_herschrijft_en_hangt_het_secret_eronder(self) -> None:
        manifest = apply_rules(_deployment("ghcr.io/org/web:1"), [GHCR])
        spec = manifest["spec"]["template"]["spec"]
        assert spec["containers"][0]["image"] == "rcr.rijksapps.nl/ghcr-rig/org/web:1"
        assert spec["imagePullSecrets"] == [{"name": "ghcr-rig-robot-pull-secret"}]

    def test_een_sidecar_uit_zijn_sjabloon_wordt_ook_geraakt(self) -> None:
        """De auth-wall proxy staat als vaste waarde in sidecar-authorization-wall.yaml.jinja
        en komt nooit langs de componentlus."""
        manifest = apply_rules(
            _deployment("ghcr.io/org/web:1", "quay.io/oauth2-proxy/oauth2-proxy:v7.7.1"), [GHCR, QUAY]
        )
        spec = manifest["spec"]["template"]["spec"]
        assert spec["containers"][1]["image"] == "rcr.rijksapps.nl/quay-rig/oauth2-proxy/oauth2-proxy:v7.7.1"
        assert {s["name"] for s in spec["imagePullSecrets"]} == {
            "ghcr-rig-robot-pull-secret",
            "quay-rig-robot-pull-secret",
        }

    def test_init_containers_tellen_mee(self) -> None:
        manifest = _deployment("ghcr.io/org/web:1")
        manifest["spec"]["template"]["spec"]["initContainers"] = [{"name": "init", "image": "quay.io/x/y:1"}]
        spec = apply_rules(manifest, [GHCR, QUAY])["spec"]["template"]["spec"]
        assert spec["initContainers"][0]["image"] == "rcr.rijksapps.nl/quay-rig/x/y:1"

    def test_een_bestaand_pull_secret_blijft_staan(self) -> None:
        manifest = _deployment("ghcr.io/org/web:1")
        manifest["spec"]["template"]["spec"]["imagePullSecrets"] = [{"name": "eigen-secret"}]
        spec = apply_rules(manifest, [GHCR])["spec"]["template"]["spec"]
        assert [s["name"] for s in spec["imagePullSecrets"]] == ["eigen-secret", "ghcr-rig-robot-pull-secret"]

    def test_hetzelfde_secret_komt_er_niet_twee_keer_in(self) -> None:
        spec = apply_rules(_deployment("ghcr.io/a:1", "ghcr.io/b:1"), [GHCR])["spec"]["template"]["spec"]
        assert spec["imagePullSecrets"] == [{"name": "ghcr-rig-robot-pull-secret"}]

    def test_de_pas_is_idempotent(self) -> None:
        """Wat de componentlus al oploste staat al op zijn bestemming; een tweede keer
        draaien mag daar niets meer aan veranderen."""
        eenmaal = apply_rules(_deployment("ghcr.io/org/web:1"), [GHCR])
        tweemaal = apply_rules(apply_rules(_deployment("ghcr.io/org/web:1"), [GHCR]), [GHCR])
        assert eenmaal == tweemaal

    def test_zonder_regels_gebeurt_er_niets(self) -> None:
        origineel = _deployment("nginx:alpine")
        assert apply_rules(_deployment("nginx:alpine"), []) == origineel

    def test_een_manifest_zonder_podspec_blijft_ongemoeid(self) -> None:
        service = {"kind": "Service", "spec": {"ports": [{"port": 80}]}}
        assert apply_rules(dict(service), [GHCR]) == service


class TestOverEenMap:
    def test_alleen_gewone_manifesten(self, tmp_path: Any) -> None:
        (tmp_path / "deployment.yaml").write_text(yaml.dump(_deployment("ghcr.io/org/web:1")))
        (tmp_path / "secret.to-sops.yaml").write_text(yaml.dump({"kind": "Secret", "stringData": {"a": "b"}}))
        (tmp_path / "kustomization.yaml").write_text(yaml.dump({"resources": []}))
        origineel_secret = (tmp_path / "secret.to-sops.yaml").read_text()

        apply_rules_to_directory(str(tmp_path), [GHCR])

        geschreven = yaml.safe_load((tmp_path / "deployment.yaml").read_text())
        assert geschreven["spec"]["template"]["spec"]["containers"][0]["image"].startswith("rcr.rijksapps.nl")
        assert (tmp_path / "secret.to-sops.yaml").read_text() == origineel_secret

    def test_een_bestand_zonder_wijziging_wordt_niet_herschreven(self, tmp_path: Any) -> None:
        """Anders herversleutelt SOPS zich suf op precies de churn die eerder is opgeruimd."""
        path = tmp_path / "service.yaml"
        path.write_text("kind: Service\nspec: {}\n")
        voor = path.stat().st_mtime_ns
        apply_rules_to_directory(str(tmp_path), [GHCR])
        assert path.stat().st_mtime_ns == voor


class TestOverEenLosDocument:
    def test_een_kale_pod_krijgt_zijn_secret(self) -> None:
        document = yaml.safe_dump(
            {"kind": "Pod", "spec": {"containers": [{"name": "backup", "image": "ghcr.io/minbzk/rig-backup:latest"}]}}
        )
        resultaat = yaml.safe_load(apply_rules_to_document(document, [GHCR]))
        assert resultaat["spec"]["containers"][0]["image"] == "rcr.rijksapps.nl/ghcr-rig/minbzk/rig-backup:latest"
        assert resultaat["spec"]["imagePullSecrets"] == [{"name": "ghcr-rig-robot-pull-secret"}]

    def test_zonder_regels_komt_de_tekst_ongewijzigd_terug(self) -> None:
        document = "kind: Pod\nspec:\n  containers: []\n"
        assert apply_rules_to_document(document, []) == document


class TestDeClustertabelIsDeBron:
    """Wat de extensie in extensions/odcn-registry-rewrite.yaml droeg, staat nu in de
    clusterconfig van de dienst -- een eigenaar voor 'welke registry, welk secret'."""

    def test_odcn_draagt_de_gedeelde_proxies(self) -> None:
        matches = {rule.match for rule in cluster_rules(ODCN)}
        assert matches == {
            "ghcr.io",
            "docker.io",
            "registry.gitlab.com",
            "gcr.io",
            "quay.io",
            "registry.k8s.io",
            "code.overheid.nl",
        }

    def test_elke_regel_noemt_zijn_secret(self) -> None:
        assert all(rule.secret and rule.to.startswith("rcr.rijksapps.nl/") for rule in cluster_rules(ODCN))

    def test_een_cluster_zonder_tabel_doet_niets(self) -> None:
        assert cluster_rules(SANDBOX) == []
