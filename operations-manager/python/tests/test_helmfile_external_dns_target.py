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

#: Een dict geldt voor elke helmfile-verwijzing, een lijst geeft er per verwijzing een.
type Values = dict[str, Any] | list[dict[str, Any]]


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


def _deployment(
    *,
    base_domain: str | None,
    subdomain: str,
    cluster: str = "odcn-production",
    references: list[str] | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {"subdomain": subdomain}
    if base_domain is not None:
        config["base-domain"] = base_domain
    return {
        "name": "production",
        "cluster": cluster,
        "namespace": "mb-docs-helmfile",
        "helmfile": [{"reference": name} for name in (references or ["docs"])],
        "services": [{"reference": "publish-on-web", "config": config}],
    }


def _wire(
    manager: Any,
    deployment: dict[str, Any],
    deployment_values: Values,
    base_values: dict[str, Any] | None = None,
    deployment_vindbaar: bool = True,
) -> None:
    """Alles rond de waardenopbouw: git-clone, values-extractie, secrets, SOPS."""
    manager.get_contents = AsyncMock(
        return_value={"name": "mb-docs-helmfile", "config": {"age-public-key": "age1demo"}}
    )
    manager.get_deployment_by_name = AsyncMock(return_value=deployment if deployment_vindbaar else None)
    manager._sops_private_key_for = AsyncMock(return_value="AGE-SECRET-KEY-PROJECT")
    manager._clone_helmfile_source = AsyncMock(return_value=("unused", None))
    manager._write_helmfile_custom_files = MagicMock(return_value={})
    manager._create_deployment_secrets = AsyncMock(return_value=[])
    manager._project_file_handler = MagicMock()
    manager._project_file_handler.get_helmfile_by_name = MagicMock(return_value={"name": "docs"})
    manager._project_file_handler.extract_helmfile_values = AsyncMock(return_value=base_values or {})
    if isinstance(deployment_values, list):
        manager._project_file_handler.extract_deployment_helmfile_values = AsyncMock(side_effect=deployment_values)
    else:
        manager._project_file_handler.extract_deployment_helmfile_values = AsyncMock(return_value=deployment_values)


async def _values(
    deployment: dict[str, Any],
    target: Path,
    deployment_values: Values,
    base_values: dict[str, Any] | None = None,
    deployment_vindbaar: bool = True,
) -> dict[str, Any]:
    manager = _manager()
    _wire(manager, deployment, deployment_values, base_values, deployment_vindbaar)
    with (
        patch("opi.manager.project_manager.get_prefixed_namespace", return_value="rig-prd-mb-docs-helmfile"),
        patch("opi.manager.project_manager.encrypt_to_sops_files_or_fail"),
    ):
        await manager._process_helmfile_deployment(deployment, MagicMock(), str(target))
    return YAML().load((target / "values.to-sops.yaml").read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_domain", "subdomain", "verwachte_target"),
    [
        pytest.param("rijksapp.nl", "docs", "router.rijksapp.nl", id="rijksapp.nl"),
        pytest.param("rijks.app", "mijn-app", "router.rijks.app", id="andere-zone-ander-doel"),
        pytest.param("rijksapp.nl", "amt.bzk", "router.rijksapp.nl", id="punt-in-het-subdomein"),
    ],
)
async def test_target_van_de_zone_landt_in_de_chart_annotaties(
    tmp_path: Path, base_domain: str, subdomain: str, verwachte_target: str
) -> None:
    deployment = _deployment(base_domain=base_domain, subdomain=subdomain)

    values = await _values(deployment, tmp_path, {})

    assert values["cluster"]["ingress"]["annotations"][TARGET_ANNOTATION] == verwachte_target


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cluster", "base_domain"),
    [
        # Geen base-domain: de hostname valt in de eigen postfix-zone van het cluster,
        # die zijn DNS via de OpenShift-router krijgt en geen expliciete target nodig heeft.
        pytest.param("odcn-production", None, id="eigen-postfix-zone"),
        # Wel een base-domain, maar geen van de zones van dit cluster draagt een target.
        pytest.param("sandboxed-local", "sandbox.rijksapp.dev", id="cluster-zonder-target"),
    ],
)
async def test_hostname_zonder_geconfigureerde_target_laat_het_blok_ongemoeid(
    tmp_path: Path, cluster: str, base_domain: str | None
) -> None:
    deployment = _deployment(base_domain=base_domain, subdomain="docs", cluster=cluster)

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
async def test_eigen_target_in_de_helmfile_basisvalues_wint(tmp_path: Path) -> None:
    """De basisvalues van de helmfile-definitie zijn ook projectvalues, dus ook die winnen."""
    deployment = _deployment(base_domain="rijksapp.nl", subdomain="docs")

    values = await _values(
        deployment,
        tmp_path,
        {},
        base_values={
            "cluster": {"ingress": {"annotations": {TARGET_ANNOTATION: "router.basis.nl", "a.io/b": "blijft"}}}
        },
    )

    assert values["cluster"]["ingress"]["annotations"] == {
        TARGET_ANNOTATION: "router.basis.nl",
        "a.io/b": "blijft",
    }


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


@pytest.mark.asyncio
async def test_elke_helmfile_verwijzing_krijgt_de_target(tmp_path: Path) -> None:
    """Elke verwijzing schrijft hetzelfde values-bestand, dus de laatste moet hem ook dragen."""
    deployment = _deployment(base_domain="rijksapp.nl", subdomain="docs", references=["docs", "static-docs"])

    values = await _values(
        deployment,
        tmp_path,
        [{}, {"cluster": {"ingress": {"annotations": {"cert-manager.io/issuer": "tweede"}}}}],
    )

    assert values["cluster"]["ingress"]["annotations"] == {
        "cert-manager.io/issuer": "tweede",
        TARGET_ANNOTATION: "router.rijksapp.nl",
    }


@pytest.mark.asyncio
async def test_zonder_afleidbare_hostname_blijft_het_blok_ongemoeid(tmp_path: Path) -> None:
    """Levert de contextopbouw geen hostname, dan gaat er geen hostname de zonelookup in."""
    deployment = _deployment(base_domain="rijksapp.nl", subdomain="docs")

    values = await _values(
        deployment,
        tmp_path,
        {"cluster": {"ingress": {"annotations": {"cert-manager.io/issuer": "eigen"}}}},
        deployment_vindbaar=False,
    )

    assert values["cluster"]["ingress"]["annotations"] == {"cert-manager.io/issuer": "eigen"}
