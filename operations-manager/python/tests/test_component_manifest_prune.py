"""Tests for pruning obsolete per-component manifests from a deployment.

Covers the pure selection helper ``_select_obsolete_component_manifests`` (a
per-component file this run did not regenerate, covering both removed components and
renamed/dropped files of surviving components) and a round-trip check that, once
obsolete files are deleted, the kustomization rebuilt from the on-disk glob no longer
references them while current components remain.
"""

import os

from opi.generation.manifests import ManifestGenerator
from opi.manager.project_manager import _select_obsolete_component_manifests
from ruamel.yaml import YAML


def _write(directory: str, name: str) -> None:
    with open(os.path.join(directory, name), "w") as f:
        f.write("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")


class TestSelectObsoleteComponentManifests:
    def test_selects_files_of_removed_component(self, tmp_path):
        directory = str(tmp_path)
        # Surviving component "profiel" (its files ARE generated this run)
        _write(directory, "profiel-deployment.yaml")
        _write(directory, "profiel-service.yaml")
        _write(directory, "profiel-platform-secret.yaml")
        # Removed component "magazijna" - all per-component variants, NOT generated
        _write(directory, "magazijna-deployment.yaml")
        _write(directory, "magazijna-service.yaml")
        _write(directory, "magazijna-platform-secret.yaml")
        _write(directory, "magazijna-user-secret.sops.yaml")
        _write(directory, "magazijna-ingress.yaml")
        _write(directory, "magazijna-ingress-api.yaml")
        _write(directory, "magazijna-data-pvc.yaml")
        _write(directory, "prod-magazijna-oauth2-cookie-secret.sops.yaml")
        # Shared / deployment-level files that must NEVER be pruned
        _write(directory, "kustomization.yaml")
        _write(directory, "decrypt-sops.yaml")
        _write(directory, "issuer-letsencrypt-rijksapps-nl.yaml")
        _write(directory, "acme-http-prod-network-policy.yaml")
        _write(directory, "prod-keycloak-secret.sops.yaml")

        selected = _select_obsolete_component_manifests(
            directory,
            component_names={"profiel", "magazijna"},
            generated_files={"profiel-deployment.yaml", "profiel-service.yaml", "profiel-platform-secret.yaml"},
            deployment_name="prod",
        )

        assert selected == sorted(
            [
                "magazijna-deployment.yaml",
                "magazijna-service.yaml",
                "magazijna-platform-secret.yaml",
                "magazijna-user-secret.sops.yaml",
                "magazijna-ingress.yaml",
                "magazijna-ingress-api.yaml",
                "magazijna-data-pvc.yaml",
                "prod-magazijna-oauth2-cookie-secret.sops.yaml",
            ]
        )

    def test_keeps_generated_cookie_secret_of_surviving_component(self, tmp_path):
        # Regression: the authorization-wall cookie secret is a per-component file named
        # with the deployment-scoped unique name
        # (<deployment>-<component>-oauth2-cookie-secret.to-sops.yaml). When a surviving
        # component generates it, project_manager must register it in created_files so
        # this prune keeps it. Before the fix it was written but NOT registered, so it was
        # pruned in the same run and the pod hung on the missing cookie secret. With the
        # file in generated_files, the prune must leave it alone.
        directory = str(tmp_path)
        _write(directory, "fundament-deployment.yaml")
        _write(directory, "productie-fundament-oauth2-cookie-secret.to-sops.yaml")

        selected = _select_obsolete_component_manifests(
            directory,
            component_names={"fundament"},
            generated_files={
                "fundament-deployment.yaml",
                "productie-fundament-oauth2-cookie-secret.to-sops.yaml",
            },
            deployment_name="productie",
        )

        assert selected == []

    def test_prunes_cookie_secret_of_surviving_component_when_not_registered(self, tmp_path):
        # Documents the exact bug shape: the cookie secret is generated for a surviving
        # component but absent from generated_files (the missing created_files.append),
        # so the prune selects the freshly written file for deletion.
        directory = str(tmp_path)
        _write(directory, "fundament-deployment.yaml")
        _write(directory, "productie-fundament-oauth2-cookie-secret.to-sops.yaml")

        selected = _select_obsolete_component_manifests(
            directory,
            component_names={"fundament"},
            generated_files={"fundament-deployment.yaml"},
            deployment_name="productie",
        )

        assert selected == ["productie-fundament-oauth2-cookie-secret.to-sops.yaml"]

    def test_selects_renamed_file_of_surviving_component(self, tmp_path):
        # The key generalisation: component "backend" stays, but its ingress path
        # changed (/api -> /), so the old "backend-ingress-api.yaml" is no longer
        # generated. It must be pruned even though backend is NOT removed.
        directory = str(tmp_path)
        _write(directory, "backend-ingress.yaml")  # new (generated this run)
        _write(directory, "backend-ingress-api.yaml")  # stale (old path, not generated)
        _write(directory, "backend-service.yaml")  # generated

        selected = _select_obsolete_component_manifests(
            directory,
            component_names={"backend"},
            generated_files={"backend-ingress.yaml", "backend-service.yaml"},
            deployment_name="staging",
        )

        assert selected == ["backend-ingress-api.yaml"]

    def test_does_not_touch_prefix_collision_with_surviving_component(self, tmp_path):
        # Removed "magazijn" must not eat surviving "magazijna" files (longest prefix wins).
        directory = str(tmp_path)
        _write(directory, "magazijn-deployment.yaml")
        _write(directory, "magazijna-deployment.yaml")

        selected = _select_obsolete_component_manifests(
            directory,
            component_names={"magazijna", "magazijn"},
            generated_files={"magazijna-deployment.yaml"},
            deployment_name="prod",
        )

        assert selected == ["magazijn-deployment.yaml"]

    def test_preserves_marked_for_deletion_pvc(self, tmp_path):
        # pvc_manager renames a removed service's PVC to *.marked-for-deletion.yaml so
        # ArgoCD keeps the volume alive during the grace period. The prune must NOT
        # hard-delete that file, or the PVC (and its data) is pruned immediately.
        directory = str(tmp_path)
        _write(directory, "magazijna-deployment.yaml")
        _write(directory, "magazijna-data-pvc.marked-for-deletion.yaml")

        selected = _select_obsolete_component_manifests(
            directory,
            component_names={"magazijna"},
            generated_files=set(),
            deployment_name="prod",
        )

        assert selected == ["magazijna-deployment.yaml"]

    def test_empty_when_all_files_generated(self, tmp_path):
        # Nothing obsolete: every per-component file on disk was generated this run.
        directory = str(tmp_path)
        _write(directory, "profiel-deployment.yaml")
        assert (
            _select_obsolete_component_manifests(
                directory,
                component_names={"profiel"},
                generated_files={"profiel-deployment.yaml"},
                deployment_name="prod",
            )
            == []
        )

    def test_ignores_shared_files_even_when_not_generated(self, tmp_path):
        # Shared/deployment-level files have no component prefix; never pruned even when
        # absent from the generated set (e.g. kustomization is written later).
        directory = str(tmp_path)
        _write(directory, "kustomization.yaml")
        _write(directory, "decrypt-sops.yaml")
        assert (
            _select_obsolete_component_manifests(
                directory, component_names={"profiel"}, generated_files=set(), deployment_name="prod"
            )
            == []
        )


class TestKustomizationAfterPrune:
    """After deleting obsolete files, the rebuilt kustomization drops them."""

    def test_rebuilt_kustomization_excludes_pruned_files(self, tmp_path):
        directory = str(tmp_path)
        _write(directory, "profiel-deployment.yaml")
        _write(directory, "profiel-service.yaml")
        _write(directory, "magazijna-deployment.yaml")
        _write(directory, "magazijna-service.yaml")

        obsolete = _select_obsolete_component_manifests(
            directory,
            component_names={"profiel", "magazijna"},
            generated_files={"profiel-deployment.yaml", "profiel-service.yaml"},
            deployment_name="prod",
        )
        for basename in obsolete:
            os.remove(os.path.join(directory, basename))

        generator = ManifestGenerator()
        assert generator.create_kustomization_files(directory, namespace="rig-prd-mpfb")

        yaml = YAML()
        with open(os.path.join(directory, "kustomization.yaml")) as f:
            kustomization = yaml.load(f)

        resources = list(kustomization.get("resources", []))
        assert "profiel-deployment.yaml" in resources
        assert "profiel-service.yaml" in resources
        assert "magazijna-deployment.yaml" not in resources
        assert "magazijna-service.yaml" not in resources
