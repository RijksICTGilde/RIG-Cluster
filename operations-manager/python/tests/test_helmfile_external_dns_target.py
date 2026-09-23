"""De helmfile-route geeft de external-dns target mee aan de chart.

De ingresses van een helmfile-deployment komen uit de chart, niet uit
``manifests/ingress.yaml.jinja``, dus de target-annotatie bereikte ze niet. Het
gevolg was een CNAME naar de router-hostname van ODC-Noord, en zo'n verwijzing
over de zonegrens overleeft de DNSSEC-validatie bij Google niet:
``docs.rijksapp.nl`` gaf SERVFAIL met EDE 12.

Git, SOPS-encryptie en de secret-generatie zijn gemockt; de clusterconfiguratie
en de hostname-afleiding zijn echt, want die dragen het oordeel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ruamel.yaml import YAML

if TYPE_CHECKING:
    from pathlib import Path

TARGET_ANNOTATION = "external-dns.alpha.kubernetes.io/target"


def _manager():
    with (
        patch("opi.manager.project_manager.KubectlConnector"),
        patch("opi.handlers.sops.SopsHandler"),
        patch("opi.generation.manifests.ManifestGenerator"),
        patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
        patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
        patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
        patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
        patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
        patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
        patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
    ):
        from opi.manager.project_manager import ProjectManager

        return ProjectManager()


def _deployment(*, base_domain: str | None, subdomain: str) -> dict[str, Any]:
    config: dict[str, Any] = {"subdomain": subdomain}
    if base_domain is not None:
        config["base-domain"] = base_domain
    return {
        "name": "production",
        "cluster": "odcn-production",
        "namespace": "mb-docs-helmfile",
        "helmfile": [{"reference": "docs"}],
        "services": [{"reference": "publish-on-web", "config": config}],
    }


def _wire(manager: Any, deployment: dict[str, Any], deployment_values: dict[str, Any]) -> None:
    """Alles rond de waardenopbouw: git-clone, values-extractie, secrets, SOPS."""
    manager.get_contents = AsyncMock(
        return_value={"name": "mb-docs-helmfile", "config": {"age-public-key": "age1demo"}}
    )
    manager.get_deployment_by_name = AsyncMock(return_value=deployment)
    manager._sops_private_key_for = AsyncMock(return_value="AGE-SECRET-KEY-PROJECT")
    manager._clone_helmfile_source = AsyncMock(return_value=("unused", None))
    manager._write_helmfile_custom_files = MagicMock(return_value={})
    manager._create_deployment_secrets = AsyncMock(return_value=[])
    manager._project_file_handler = MagicMock()
    manager._project_file_handler.get_helmfile_by_name = MagicMock(return_value={"name": "docs"})
    manager._project_file_handler.extract_helmfile_values = AsyncMock(return_value={})
    manager._project_file_handler.extract_deployment_helmfile_values = AsyncMock(return_value=deployment_values)


async def _values(deployment: dict[str, Any], target: Path, deployment_values: dict[str, Any]) -> dict[str, Any]:
    manager = _manager()
    _wire(manager, deployment, deployment_values)
    with (
        patch("opi.manager.project_manager.get_prefixed_namespace", return_value="rig-prd-mb-docs-helmfile"),
        patch("opi.manager.project_manager.encrypt_to_sops_files_or_fail"),
    ):
        await manager._process_helmfile_deployment(deployment, MagicMock(), str(target))
    return YAML().load((target / "values.to-sops.yaml").read_text())


@pytest.mark.asyncio
async def test_target_van_de_zone_landt_in_de_chart_annotaties(tmp_path: Path) -> None:
    deployment = _deployment(base_domain="rijksapp.nl", subdomain="docs")

    values = await _values(deployment, tmp_path, {})

    assert values["cluster"]["ingress"]["annotations"][TARGET_ANNOTATION] == "router.rijksapp.nl"


@pytest.mark.asyncio
async def test_hostname_zonder_geconfigureerde_target_laat_het_blok_ongemoeid(tmp_path: Path) -> None:
    # Geen base-domain: de hostname valt in de eigen postfix-zone van het cluster,
    # die zijn DNS via de OpenShift-router krijgt en geen expliciete target nodig heeft.
    deployment = _deployment(base_domain=None, subdomain="docs")

    values = await _values(
        deployment, tmp_path, {"cluster": {"ingress": {"annotations": {"cert-manager.io/issuer": "eigen"}}}}
    )

    assert values["cluster"]["ingress"]["annotations"] == {"cert-manager.io/issuer": "eigen"}


@pytest.mark.asyncio
async def test_eigen_target_in_de_projectvalues_wint(tmp_path: Path) -> None:
    deployment = _deployment(base_domain="rijksapp.nl", subdomain="docs")

    values = await _values(
        deployment,
        tmp_path,
        {"cluster": {"ingress": {"annotations": {TARGET_ANNOTATION: "router.eigen.nl"}}}},
    )

    assert values["cluster"]["ingress"]["annotations"][TARGET_ANNOTATION] == "router.eigen.nl"


@pytest.mark.asyncio
async def test_bestaande_annotaties_blijven_naast_de_nieuwe_staan(tmp_path: Path) -> None:
    deployment = _deployment(base_domain="rijksapp.nl", subdomain="docs")

    values = await _values(
        deployment,
        tmp_path,
        {
            "cluster": {
                "ingress": {
                    "className": None,
                    "annotations": {
                        "cert-manager.io/issuer": "letsencrypt-mb-docs-helmfile",
                        "haproxy.router.openshift.io/ip_whitelist": "10.0.0.0/8",
                    },
                }
            }
        },
    )

    assert values["cluster"]["ingress"]["annotations"] == {
        "cert-manager.io/issuer": "letsencrypt-mb-docs-helmfile",
        "haproxy.router.openshift.io/ip_whitelist": "10.0.0.0/8",
        TARGET_ANNOTATION: "router.rijksapp.nl",
    }
    assert "className" in values["cluster"]["ingress"]
