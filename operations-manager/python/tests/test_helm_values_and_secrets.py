"""Direct measurements of the two Helm helpers that every Helm test stubs away.

``_get_helm_values_context`` and ``_create_deployment_secrets`` have no test of their own:
the three tests on ``_process_helm_chart_deployment`` and ``_process_helmfile_deployment``
replace exactly these two with a mock, so no test runs the Helm path with a real values
context and real secret manifests. Both are measured here on a real instance, with the real
``ManifestGenerator`` writing into a tmp directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from opi.generation.manifests import ManifestGenerator
from opi.manager.project_manager import ProjectManager
from opi.services.secrets import DatabaseSecret, KeycloakSecret, MinIOSecret, RedisSecret
from opi.utils.sops import sops_filenames
from ruamel.yaml import YAML

if TYPE_CHECKING:
    from pathlib import Path

CLUSTER = "sandboxed-local"
INGRESS_POSTFIX = ".sandbox.rijksapp.dev"
PROJECT = {"name": "demo"}


def _manager(deployment: dict[str, Any] | None, secrets: dict[str, Any] | None = None) -> ProjectManager:
    """A half-built manager: only what these two helpers reach."""
    manager = ProjectManager.__new__(ProjectManager)
    manager._secrets_to_create = {"production": dict(secrets)} if secrets else {}
    manager._manifest_generator = ManifestGenerator()
    manager.get_deployment_by_name = AsyncMock(return_value=deployment)
    manager.get_contents = AsyncMock(return_value=PROJECT)
    return manager


def _deployment(**overrides: Any) -> dict[str, Any]:
    return {"name": "production", "cluster": CLUSTER, "namespace": "demo", **overrides}


def _database_secret() -> DatabaseSecret:
    return DatabaseSecret(
        host="rig-db-rw.rig-db",
        port=5432,
        username="demo_user",
        password="db-dummy-geheim",
        database="demo",
        schema="demo_production",
    )


def _minio_secret() -> MinIOSecret:
    return MinIOSecret(
        host="minio.rig-system",
        port=9000,
        access_key="demo-access",
        secret_key="minio-geheim",
        bucket_name="demo-production",
    )


def _keycloak_secret() -> KeycloakSecret:
    return KeycloakSecret(
        client_id="demo-production",
        client_secret="oidc-geheim",
        public_client_id="demo-production-public",
        discovery_url="https://keycloak.example/realms/demo/.well-known/openid-configuration",
        base_url="https://keycloak.example",
        realm="demo",
    )


def _redis_secret() -> RedisSecret:
    return RedisSecret(
        host="redis.rig-system", port=6379, username="demo", password="redis-dummy-geheim", key_prefix="demo-production"
    )


class TestHelmValuesContext:
    """The alias context a Helm or helmfile deployment resolves its values against."""

    @pytest.mark.asyncio
    async def test_an_unknown_deployment_yields_an_empty_context(self) -> None:
        assert await _manager(None)._get_helm_values_context("production") == {}

    @pytest.mark.asyncio
    async def test_without_a_subdomain_the_hostname_falls_back_to_the_deployment_name(self) -> None:
        """On the cluster domain the name carries a double dot, which is what the code does
        today: ``get_ingress_postfix`` already starts with one and this path adds another."""
        context = await _manager(_deployment())._get_helm_values_context("production")

        assert context["HOSTNAME"] == f"production.{INGRESS_POSTFIX}"
        assert context["PUBLIC_HOST"] == f"https://production.{INGRESS_POSTFIX}"
        assert "SUBDOMAIN" not in context
        assert "BASE_DOMAIN" not in context

    @pytest.mark.asyncio
    async def test_a_subdomain_on_the_cluster_domain(self) -> None:
        """Same double dot as the fallback above; pinned, not endorsed."""
        context = await _manager(_deployment(subdomain="app"))._get_helm_values_context("production")

        assert context["HOSTNAME"] == f"app.{INGRESS_POSTFIX}"
        assert context["PUBLIC_HOSTNAME"] == f"app.{INGRESS_POSTFIX}"
        assert context["SUBDOMAIN"] == "app"

    @pytest.mark.asyncio
    async def test_a_subdomain_on_an_own_base_domain(self) -> None:
        deployment = _deployment(subdomain="app", **{"base-domain": "voorbeeld.nl"})
        context = await _manager(deployment)._get_helm_values_context("production")

        assert context["HOSTNAME"] == "app.voorbeeld.nl"
        assert context["PUBLIC_HOST"] == "https://app.voorbeeld.nl"
        assert context["BASE_DOMAIN"] == "voorbeeld.nl"

    @pytest.mark.asyncio
    async def test_a_letsencrypt_issuer_on_an_own_domain_gets_the_generated_name(self) -> None:
        """The name must match the Issuer resource the manifest generation creates."""
        deployment = _deployment(subdomain="app", **{"base-domain": "voorbeeld.nl", "issuer": "letsencrypt"})
        context = await _manager(deployment)._get_helm_values_context("production")

        assert context["ISSUER"] == "letsencrypt-voorbeeld-nl-production"

    @pytest.mark.asyncio
    async def test_any_other_issuer_is_passed_through_unchanged(self) -> None:
        deployment = _deployment(subdomain="app", issuer="eigen-issuer")
        context = await _manager(deployment)._get_helm_values_context("production")

        assert context["ISSUER"] == "eigen-issuer"

    @pytest.mark.asyncio
    async def test_every_service_credential_lands_in_the_context(self) -> None:
        manager = _manager(
            _deployment(subdomain="app"),
            {
                "database": _database_secret(),
                "minio": _minio_secret(),
                "keycloak": _keycloak_secret(),
                "redis": _redis_secret(),
            },
        )

        context = await manager._get_helm_values_context("production")

        assert context["DATABASE_SERVER_HOST"] == "rig-db-rw.rig-db"
        assert context["DATABASE_SERVER_USER"] == "demo_user"
        assert context["DATABASE_PASSWORD"] == "db-dummy-geheim"
        assert context["DATABASE_DB"] == "demo"
        assert context["DATABASE_SCHEMA"] == "demo_production"
        assert context["OBJECT_STORE_URL"] == "minio.rig-system:9000"
        assert context["OBJECT_STORE_ENDPOINT_URL"] == "http://minio.rig-system:9000"
        assert context["OBJECT_STORE_USER"] == "demo-access"
        assert context["OBJECT_STORE_BUCKET_NAME"] == "demo-production"
        assert context["OIDC_CLIENT_ID"] == "demo-production"
        assert context["OIDC_CLIENT_SECRET"] == "oidc-geheim"
        assert context["KEYCLOAK_REALM"] == "demo"
        assert context["REDIS_HOST"] == "redis.rig-system"
        assert context["REDIS_URL"] == "redis://demo:redis-dummy-geheim@redis.rig-system:6379/0"

    @pytest.mark.asyncio
    async def test_ports_stay_integers(self) -> None:
        """A quoted port in a values file is a type error in the chart, not a cosmetic one."""
        manager = _manager(
            _deployment(), {"database": _database_secret(), "minio": _minio_secret(), "redis": _redis_secret()}
        )

        context = await manager._get_helm_values_context("production")

        assert context["DATABASE_SERVER_PORT"] == 5432
        assert context["OBJECT_STORE_PORT"] == 9000
        assert context["REDIS_PORT"] == 6379
        for key in ("DATABASE_SERVER_PORT", "OBJECT_STORE_PORT", "REDIS_PORT"):
            assert isinstance(context[key], int), key

    @pytest.mark.asyncio
    async def test_a_deployment_without_services_gets_no_credentials(self) -> None:
        context = await _manager(_deployment())._get_helm_values_context("production")

        assert not [key for key in context if key.startswith(("DATABASE_", "OBJECT_STORE_", "OIDC_", "REDIS_"))]


class TestDeploymentSecrets:
    """The Secret manifests a Helm deployment gets for the services it uses."""

    @staticmethod
    def _pairs(target: Path, filename: str) -> dict[str, str]:
        return YAML(typ="safe").load((target / filename).read_text())["stringData"]

    @pytest.mark.asyncio
    async def test_without_secrets_nothing_is_written(self, tmp_path: Path) -> None:
        created = await _manager(_deployment())._create_deployment_secrets(
            "production", str(tmp_path), "rig-demo", CLUSTER
        )

        assert created == []
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_one_manifest_per_service_with_the_plaintext_name(self, tmp_path: Path) -> None:
        manager = _manager(
            _deployment(),
            {
                "keycloak": _keycloak_secret(),
                "database": _database_secret(),
                "minio": _minio_secret(),
                "redis": _redis_secret(),
            },
        )

        created = await manager._create_deployment_secrets("production", str(tmp_path), "rig-demo", CLUSTER)

        assert created == [
            sops_filenames(f"production-{service}-secret").plaintext
            for service in ("keycloak", "database", "minio", "redis")
        ]
        assert sorted(path.name for path in tmp_path.iterdir()) == sorted(created)

    @pytest.mark.asyncio
    async def test_the_written_pairs_are_the_credentials_of_that_service(self, tmp_path: Path) -> None:
        manager = _manager(_deployment(), {"database": _database_secret()})

        created = await manager._create_deployment_secrets("production", str(tmp_path), "rig-demo", CLUSTER)

        pairs = self._pairs(tmp_path, created[0])
        assert pairs == _database_secret().to_k8s_secret_data()
        assert pairs["DATABASE_PASSWORD"] == "db-dummy-geheim"

    @pytest.mark.asyncio
    async def test_the_namespace_is_the_one_passed_in(self, tmp_path: Path) -> None:
        manager = _manager(_deployment(), {"redis": _redis_secret()})

        created = await manager._create_deployment_secrets("production", str(tmp_path), "rig-demo", CLUSTER)

        manifest = YAML(typ="safe").load((tmp_path / created[0]).read_text())
        assert manifest["metadata"]["namespace"] == "rig-demo"
        assert manifest["metadata"]["name"] == "production-redis"

    @pytest.mark.asyncio
    async def test_only_the_services_in_the_map_get_a_manifest(self, tmp_path: Path) -> None:
        manager = _manager(_deployment(), {"minio": _minio_secret()})

        created = await manager._create_deployment_secrets("production", str(tmp_path), "rig-demo", CLUSTER)

        assert created == [sops_filenames("production-minio-secret").plaintext]
