"""Een kloon erft de VORM van het webadres van zijn bron, niet de NAAM (RC-217).

De naamvelden mogen niet mee: dan claimen twee deployments dezelfde hostnaam. Een
ontbrekende ``domain-format`` is geen fout, dus wissen gaf de kloon niet "geen vorm" maar
de platformdefault van het moment van verwerken. Zie
``features/kloon-erft-de-vorm-van-het-webadres.md``.

Daarom meet dit bestand de hostnaam en niet alleen het veld: de kloon hoort evenveel
adressen op te leveren als zijn bron.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.catalog.publish_on_web.urls import public_url_map_for_deployment

CLUSTER = "local"
PROJECT = "asses-k2n"


def _make_manager():
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


def _project(source_config: dict[str, Any]) -> dict[str, Any]:
    """Een project met twee componenten die allebei publiceren.

    Twee, want met één component is elke vorm evenveel adressen en meet de test niets.
    """
    source: dict[str, Any] = {
        "name": "productie",
        "cluster": CLUSTER,
        "namespace": PROJECT,
        "components": [
            {"reference": "spa", "image": "ghcr.io/org/spa:v1"},
            {"reference": "api", "image": "ghcr.io/org/api:v1"},
        ],
    }
    if source_config:
        source["services"] = [{"reference": "publish-on-web", "config": dict(source_config)}]
    return {
        "name": PROJECT,
        "clusters": [CLUSTER],
        "repositories": [{"name": "main-repo"}],
        "components": [
            {
                "name": "spa",
                "type": "frontend",
                "ports": {"inbound": [8080], "outbound": [443]},
                "path": "/",
                "services": ["publish-on-web"],
            },
            {
                "name": "api",
                "type": "single",
                "ports": {"inbound": [8000], "outbound": [443]},
                "path": [{"match": "/api"}],
                "services": ["publish-on-web"],
            },
        ],
        "deployments": [source],
    }


async def _clone(project_data: dict[str, Any], clone_name: str = "pr-857", **requested: str) -> dict[str, Any]:
    """Kloon ``productie`` naar ``clone_name`` en geef de nieuwe deployment terug.

    ``requested`` is wat de beller zelf meegeeft (``domain_format``, ``subdomain``), dus
    naast wat er van de bron komt.
    """
    pm = _make_manager()
    pm.get_contents = AsyncMock(return_value=project_data)
    pm.get_name = AsyncMock(return_value=PROJECT)
    pm.get_deployments = AsyncMock(return_value=project_data["deployments"])
    pm._validate_component_references = MagicMock(return_value={"success": True, "error": None})
    pm.save_and_commit_project = AsyncMock()

    with patch("opi.manager.project_manager.ensure_domain_requests"):
        result = await pm.upsert_deployment(
            deployment_name=clone_name,
            components=[
                SimpleNamespace(reference="spa", image=f"ghcr.io/org/spa:{clone_name}"),
                SimpleNamespace(reference="api", image=f"ghcr.io/org/api:{clone_name}"),
            ],
            clone_from="productie",
            **requested,
        )

    assert result["success"] is True, result
    return next(d for d in project_data["deployments"] if d["name"] == clone_name)


def _hostnames(project_data: dict[str, Any], deployment: dict[str, Any]) -> set[str]:
    """De hostnamen die deze deployment publiceert, zoals de portal en de API ze lezen."""
    urls = public_url_map_for_deployment(project_data, deployment, PROJECT, ProjectFileHandler())
    assert urls, f"geen enkel publiek adres voor '{deployment['name']}'; de meting zegt dan niets"
    return {url.split("://", 1)[-1].split("/", 1)[0] for url in urls.values()}


class TestDeVormReistMee:
    async def test_de_kloon_houdt_de_domain_format_van_de_bron(self) -> None:
        project_data = _project({"domain-format": "deployment-project"})

        clone = await _clone(project_data)

        assert get_domain_setting(clone, DomainSetting.DOMAIN_FORMAT) == "deployment-project"

    async def test_de_kloon_publiceert_evenveel_adressen_als_zijn_bron(self) -> None:
        """De assertie die asses-k2n had gevangen: niet het veld, maar de uitkomst.

        ``deployment-project`` geeft de hele deployment een adres, de default van vandaag
        ieder component een eigen, en dan is ``/api`` op het adres van de SPA weg.
        """
        project_data = _project({"domain-format": "deployment-project"})
        source = project_data["deployments"][0]

        clone = await _clone(project_data)

        assert _hostnames(project_data, source) == {f"productie-{PROJECT}.kind"}
        assert _hostnames(project_data, clone) == {f"pr-857-{PROJECT}.kind"}

    async def test_zonder_die_vorm_valt_de_kloon_uiteen_in_twee_adressen(self) -> None:
        """De tegenproef bij de toets hierboven, en tevens wat een kloon van een bron
        zonder ``domain-format`` vandaag krijgt."""
        project_data = _project({})

        clone = await _clone(project_data)

        assert _hostnames(project_data, clone) == {
            f"spa-pr-857-{PROJECT}.kind",
            f"api-pr-857-{PROJECT}.kind",
        }

    async def test_de_kloon_erft_de_naam_van_de_bron_niet(self) -> None:
        project_data = _project(
            {
                "domain-format": "deployment-project",
                "base-domain": "rijksapps.nl",
                "subdomain": "assessment",
                "issuer": "letsencrypt",
            }
        )

        clone = await _clone(project_data)

        assert get_domain_setting(clone, DomainSetting.BASE_DOMAIN) is None
        assert get_domain_setting(clone, DomainSetting.SUBDOMAIN) is None
        assert get_domain_setting(clone, DomainSetting.ISSUER) is None
        assert get_domain_setting(clone, DomainSetting.DOMAIN_FORMAT) == "deployment-project"

    async def test_een_bron_zonder_vorm_verandert_niet(self) -> None:
        """De grens van deze PR: bij een lege ``domain-format`` valt er niets over te nemen.

        Vastgelegd omdat een latere pin van de effectieve vorm deze verwachting hoort om te
        gooien, in plaats van er stil langsheen te lopen.
        """
        project_data = _project({})

        clone = await _clone(project_data)

        assert get_domain_setting(clone, DomainSetting.DOMAIN_FORMAT) is None
        assert clone.get("services") is None


class TestEenVormDieZijnNaamNodigHeeftReistNietMee:
    @pytest.mark.parametrize(
        "domain_format",
        [
            "component.subdomain",
            "deployment.project",
            "deployment-subdomain",  # {subdomain} rendert leeg: 'pr-857-.kind'
            "subdomain",
        ],
    )
    async def test_de_vorm_wordt_gewist_als_hij_op_de_naam_leunt(self, domain_format: str) -> None:
        project_data = _project(
            {
                "domain-format": domain_format,
                "base-domain": "rijks.app",
                "subdomain": "assessment",
            }
        )

        clone = await _clone(project_data)

        assert get_domain_setting(clone, DomainSetting.DOMAIN_FORMAT) is None
        assert not any(host.count(".") > 1 for host in _hostnames(project_data, clone))
        assert not any("-." in host for host in _hostnames(project_data, clone))

    async def test_geen_lege_dienstingang_blijft_achter(self) -> None:
        """Valt de vorm weg en is de naam gewist, dan houdt de kloon geen leeg record over."""
        project_data = _project({"domain-format": "component.subdomain", "subdomain": "assessment"})

        clone = await _clone(project_data)

        assert clone.get("services") is None


class TestDeEigenVraagVanDeBellerWint:
    """De nieuwe grendel gaat over de vorm die de kloon ERFT, niet over de vorm die hij VRAAGT.

    De eigen aanvraag wordt na het wissen teruggeschreven, dus ze staat los van de bron.
    """

    async def test_de_gevraagde_vorm_wint_van_die_van_de_bron(self) -> None:
        project_data = _project({"domain-format": "deployment-project"})

        clone = await _clone(project_data, domain_format="component-deployment-project")

        assert get_domain_setting(clone, DomainSetting.DOMAIN_FORMAT) == "component-deployment-project"
        assert _hostnames(project_data, clone) == {
            f"spa-pr-857-{PROJECT}.kind",
            f"api-pr-857-{PROJECT}.kind",
        }

    async def test_een_gevraagde_vorm_die_op_de_naam_leunt_blijft_staan(self) -> None:
        """Hij leunt op een naam die de beller er zelf bij levert, dus er valt niets weg."""
        project_data = _project({"domain-format": "deployment-project"})

        clone = await _clone(project_data, domain_format="component-subdomain", subdomain="pr-857")

        assert get_domain_setting(clone, DomainSetting.DOMAIN_FORMAT) == "component-subdomain"
        assert _hostnames(project_data, clone) == {"spa-pr-857.kind", "api-pr-857.kind"}
