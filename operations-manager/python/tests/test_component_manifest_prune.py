"""Tests for pruning manifests of components removed from a deployment.

Covers the pure selection helper ``_select_orphaned_component_manifests`` and a
round-trip check that, once orphan files are deleted, the kustomization rebuilt
from the on-disk glob no longer references them while current components remain.
"""

import os

from opi.generation.manifests import ManifestGenerator
from opi.manager.project_manager import _select_orphaned_component_manifests
from ruamel.yaml import YAML


def _write(directory: str, name: str) -> None:
    with open(os.path.join(directory, name), "w") as f:
        f.write("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")


class TestSelectOrphanedComponentManifests:
    def test_selects_only_removed_component_files(self, tmp_path):
        directory = str(tmp_path)
        # Surviving component "profiel"
        _write(directory, "profiel-deployment.yaml")
        _write(directory, "profiel-service.yaml")
        _write(directory, "profiel-platform-secret.yaml")
        # Removed component "magazijna" - all per-component variants
        _write(directory, "magazijna-deployment.yaml")
        _write(directory, "magazijna-service.yaml")
        _write(directory, "magazijna-platform-secret.yaml")
        _write(directory, "magazijna-user-secret.sops.yaml")
        _write(directory, "magazijna-ingress.yaml")
        _write(directory, "magazijna-ingress-api.yaml")
        _write(directory, "magazijna-data-pvc.yaml")
        # oauth2 cookie secret uses deployment-scoped unique-name prefix
        _write(directory, "prod-magazijna-oauth2-cookie-secret.sops.yaml")
        # Shared / deployment-level files that must NEVER be pruned
        _write(directory, "kustomization.yaml")
        _write(directory, "decrypt-sops.yaml")
        _write(directory, "issuer-letsencrypt-rijksapps-nl.yaml")
        _write(directory, "acme-http-prod-network-policy.yaml")
        _write(directory, "prod-keycloak-secret.sops.yaml")

        selected = _select_orphaned_component_manifests(
            directory,
            keep_component_names={"profiel"},
            removed_component_names={"magazijna"},
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

    def test_does_not_touch_prefix_collision_with_surviving_component(self, tmp_path):
        # Removed "magazijn" must not eat surviving "magazijna" files (longest prefix wins).
        directory = str(tmp_path)
        _write(directory, "magazijn-deployment.yaml")
        _write(directory, "magazijna-deployment.yaml")

        selected = _select_orphaned_component_manifests(
            directory,
            keep_component_names={"magazijna"},
            removed_component_names={"magazijn"},
            deployment_name="prod",
        )

        assert selected == ["magazijn-deployment.yaml"]

    def test_preserves_marked_for_deletion_pvc(self, tmp_path):
        # pvc_manager renames a removed component's PVC to *.marked-for-deletion.yaml
        # so ArgoCD keeps the volume alive during the grace period. The prune must
        # NOT hard-delete that file, or the PVC (and its data) is pruned immediately.
        directory = str(tmp_path)
        _write(directory, "magazijna-deployment.yaml")
        _write(directory, "magazijna-data-pvc.marked-for-deletion.yaml")

        selected = _select_orphaned_component_manifests(
            directory,
            keep_component_names=set(),
            removed_component_names={"magazijna"},
            deployment_name="prod",
        )

        assert selected == ["magazijna-deployment.yaml"]

    def test_empty_when_nothing_removed(self, tmp_path):
        directory = str(tmp_path)
        _write(directory, "profiel-deployment.yaml")
        assert (
            _select_orphaned_component_manifests(
                directory, keep_component_names={"profiel"}, removed_component_names=set(), deployment_name="prod"
            )
            == []
        )


class TestKustomizationAfterPrune:
    """After deleting orphan files, the rebuilt kustomization drops them."""

    def test_rebuilt_kustomization_excludes_pruned_files(self, tmp_path):
        directory = str(tmp_path)
        _write(directory, "profiel-deployment.yaml")
        _write(directory, "profiel-service.yaml")
        _write(directory, "magazijna-deployment.yaml")
        _write(directory, "magazijna-service.yaml")

        orphans = _select_orphaned_component_manifests(
            directory,
            keep_component_names={"profiel"},
            removed_component_names={"magazijna"},
            deployment_name="prod",
        )
        for basename in orphans:
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
