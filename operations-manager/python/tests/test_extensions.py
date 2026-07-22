import os
from typing import ClassVar

import pytest
import yaml
from opi.extensions.pipeline import ExtensionPipeline, _load_extension_definition, load_extensions
from opi.extensions.registry_rewrite import RegistryRewriteExtension

# ---------------------------------------------------------------------------
# RegistryRewriteExtension
# ---------------------------------------------------------------------------


class TestRegistryRewriteExtension:
    def _make_extension(self, mappings: list[dict]) -> RegistryRewriteExtension:
        return RegistryRewriteExtension({"mappings": mappings})

    def _deployment(self, image: str, pull_secrets: list[dict] | None = None) -> dict:
        pod_spec: dict = {
            "containers": [{"name": "app", "image": image}],
        }
        if pull_secrets is not None:
            pod_spec["imagePullSecrets"] = pull_secrets
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "test"},
            "spec": {"template": {"spec": pod_spec}},
        }

    def test_rewrites_ghcr_image(self) -> None:
        ext = self._make_extension(
            [
                {"from": "ghcr.io", "to": "rcr.rijksapps.nl/ghcr-rig", "imagePullSecret": "ghcr-secret"},
            ]
        )
        manifest = self._deployment("ghcr.io/org/app:latest")
        result = ext.process(manifest)

        container = result["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "rcr.rijksapps.nl/ghcr-rig/org/app:latest"
        assert result["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "ghcr-secret"}]

    def test_rewrites_bare_pod_image_and_adds_pull_secret(self) -> None:
        """Backup pods are bare Pods (spec at .spec, not .spec.template.spec) - they
        must be rewritten too, otherwise they keep raw ghcr.io refs and fail on ODCN."""
        ext = self._make_extension(
            [{"from": "ghcr.io", "to": "rcr.rijksapps.nl/ghcr-rig", "imagePullSecret": "ghcr-rig-robot-pull-secret"}]
        )
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "db-backup-test"},
            "spec": {"containers": [{"name": "backup", "image": "ghcr.io/minbzk/base-images/rig-backup:latest"}]},
        }
        result = ext.process(manifest)

        assert (
            result["spec"]["containers"][0]["image"] == "rcr.rijksapps.nl/ghcr-rig/minbzk/base-images/rig-backup:latest"
        )
        assert result["spec"]["imagePullSecrets"] == [{"name": "ghcr-rig-robot-pull-secret"}]

    def test_no_rewrite_for_unmatched_image(self) -> None:
        ext = self._make_extension(
            [
                {"from": "ghcr.io", "to": "rcr.rijksapps.nl/ghcr-rig", "imagePullSecret": "ghcr-secret"},
            ]
        )
        manifest = self._deployment("quay.io/org/app:v1")
        result = ext.process(manifest)

        container = result["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "quay.io/org/app:v1"
        assert "imagePullSecrets" not in result["spec"]["template"]["spec"]

    def test_deduplicates_existing_pull_secrets(self) -> None:
        ext = self._make_extension(
            [
                {"from": "ghcr.io", "to": "rcr.rijksapps.nl/ghcr-rig", "imagePullSecret": "ghcr-secret"},
            ]
        )
        manifest = self._deployment(
            "ghcr.io/org/app:latest",
            pull_secrets=[{"name": "ghcr-secret"}],
        )
        result = ext.process(manifest)

        secrets = result["spec"]["template"]["spec"]["imagePullSecrets"]
        assert secrets == [{"name": "ghcr-secret"}]

    def test_merges_with_existing_pull_secrets(self) -> None:
        ext = self._make_extension(
            [
                {"from": "ghcr.io", "to": "rcr.rijksapps.nl/ghcr-rig", "imagePullSecret": "ghcr-secret"},
            ]
        )
        manifest = self._deployment(
            "ghcr.io/org/app:latest",
            pull_secrets=[{"name": "other-secret"}],
        )
        result = ext.process(manifest)

        secrets = result["spec"]["template"]["spec"]["imagePullSecrets"]
        assert len(secrets) == 2
        names = {s["name"] for s in secrets}
        assert names == {"other-secret", "ghcr-secret"}

    def test_rewrites_multiple_containers(self) -> None:
        ext = self._make_extension(
            [
                {"from": "ghcr.io", "to": "rcr.rijksapps.nl/ghcr-rig", "imagePullSecret": "ghcr-secret"},
            ]
        )
        manifest = self._deployment("ghcr.io/org/app:latest")
        manifest["spec"]["template"]["spec"]["initContainers"] = [
            {"name": "init", "image": "ghcr.io/org/init:v1"},
        ]
        result = ext.process(manifest)

        app = result["spec"]["template"]["spec"]["containers"][0]
        init = result["spec"]["template"]["spec"]["initContainers"][0]
        assert app["image"] == "rcr.rijksapps.nl/ghcr-rig/org/app:latest"
        assert init["image"] == "rcr.rijksapps.nl/ghcr-rig/org/init:v1"

    def test_multiple_mappings(self) -> None:
        ext = self._make_extension(
            [
                {"from": "ghcr.io", "to": "rcr.rijksapps.nl/ghcr-rig", "imagePullSecret": "ghcr-secret"},
                {"from": "docker.io", "to": "rcr.rijksapps.nl/dockerhub-rig", "imagePullSecret": "docker-secret"},
            ]
        )
        manifest = self._deployment("docker.io/library/nginx:latest")
        result = ext.process(manifest)

        container = result["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "rcr.rijksapps.nl/dockerhub-rig/library/nginx:latest"
        assert result["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "docker-secret"}]

    def test_statefulset(self) -> None:
        ext = self._make_extension(
            [
                {"from": "ghcr.io", "to": "mirror.example.com/ghcr", "imagePullSecret": "secret"},
            ]
        )
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": "db"},
            "spec": {"template": {"spec": {"containers": [{"name": "db", "image": "ghcr.io/org/db:v1"}]}}},
        }
        result = ext.process(manifest)
        assert result["spec"]["template"]["spec"]["containers"][0]["image"] == "mirror.example.com/ghcr/org/db:v1"

    def test_cronjob(self) -> None:
        ext = self._make_extension(
            [
                {"from": "ghcr.io", "to": "mirror.example.com/ghcr", "imagePullSecret": "secret"},
            ]
        )
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "job"},
            "spec": {
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {"containers": [{"name": "worker", "image": "ghcr.io/org/worker:v1"}]},
                        },
                    },
                },
            },
        }
        result = ext.process(manifest)
        pod_spec = result["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        assert pod_spec["containers"][0]["image"] == "mirror.example.com/ghcr/org/worker:v1"

    def test_ignores_non_workload_kinds(self) -> None:
        ext = self._make_extension(
            [
                {"from": "ghcr.io", "to": "mirror.example.com/ghcr", "imagePullSecret": "secret"},
            ]
        )
        manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "svc"},
            "spec": {"ports": [{"port": 80}]},
        }
        result = ext.process(manifest)
        assert result == manifest

    def test_mapping_without_pull_secret(self) -> None:
        ext = self._make_extension(
            [
                {"from": "ghcr.io", "to": "rcr.rijksapps.nl/ghcr-rig"},
            ]
        )
        manifest = self._deployment("ghcr.io/org/app:latest")
        result = ext.process(manifest)

        container = result["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "rcr.rijksapps.nl/ghcr-rig/org/app:latest"
        assert "imagePullSecrets" not in result["spec"]["template"]["spec"]


# ---------------------------------------------------------------------------
# ExtensionPipeline
# ---------------------------------------------------------------------------


class TestExtensionPipeline:
    def test_empty_pipeline(self) -> None:
        pipeline = ExtensionPipeline([])
        assert not pipeline.has_extensions

    def test_process_directory(self, tmp_path: str) -> None:
        """Pipeline processes .yaml files and skips sops/kustomization files."""
        ext = RegistryRewriteExtension(
            {
                "mappings": [{"from": "ghcr.io", "to": "mirror.local/ghcr", "imagePullSecret": "secret"}],
            }
        )
        pipeline = ExtensionPipeline([ext])

        # Write a deployment manifest
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "test"},
            "spec": {"template": {"spec": {"containers": [{"name": "app", "image": "ghcr.io/org/app:v1"}]}}},
        }
        deploy_path = os.path.join(tmp_path, "deployment.yaml")
        with open(deploy_path, "w") as f:
            yaml.dump(deployment, f)

        # Write a file that should be skipped
        sops_path = os.path.join(tmp_path, "secrets.sops.yaml")
        with open(sops_path, "w") as f:
            yaml.dump({"sops": "encrypted"}, f)

        kustomization_path = os.path.join(tmp_path, "kustomization.yaml")
        with open(kustomization_path, "w") as f:
            yaml.dump({"resources": ["deployment.yaml"]}, f)

        pipeline.process_directory(str(tmp_path))

        # Verify deployment was modified
        with open(deploy_path) as f:
            result = yaml.safe_load(f)
        assert result["spec"]["template"]["spec"]["containers"][0]["image"] == "mirror.local/ghcr/org/app:v1"

        # Verify sops file was not touched
        with open(sops_path) as f:
            result = yaml.safe_load(f)
        assert result == {"sops": "encrypted"}


# ---------------------------------------------------------------------------
# load_extensions
# ---------------------------------------------------------------------------


class TestLoadExtensions:
    def test_load_for_cluster_without_extensions(self) -> None:
        pipeline = load_extensions("local")
        assert not pipeline.has_extensions

    def test_load_for_odcn_production(self) -> None:
        pipeline = load_extensions("odcn-production")
        assert pipeline.has_extensions

    def test_unknown_extension_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            _load_extension_definition("nonexistent-extension")


class TestOriginalImage:
    """Reverse of the rewrite: show the source registry, not the rcr proxy."""

    _MAPPINGS: ClassVar[list[dict[str, str]]] = [
        {"from": "code.overheid.nl", "to": "rcr.rijksapps.nl/code-overheid-rig"},
        {"from": "ghcr.io", "to": "rcr.rijksapps.nl/ghcr-rig"},
    ]

    def test_reverses_proxy_to_source_registry(self) -> None:
        from opi.extensions.registry_rewrite import original_image

        assert (
            original_image("rcr.rijksapps.nl/code-overheid-rig/robbertbos/app:2026.6.30", self._MAPPINGS)
            == "code.overheid.nl/robbertbos/app:2026.6.30"
        )

    def test_unmatched_image_is_unchanged(self) -> None:
        from opi.extensions.registry_rewrite import original_image

        # RIG's own registry has no mirror mapping -> already the real repo.
        assert original_image("rcr.rijksapps.nl/rig/zad:desa-16", self._MAPPINGS) == "rcr.rijksapps.nl/rig/zad:desa-16"
