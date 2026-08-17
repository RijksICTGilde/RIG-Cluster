"""Een refresh noemt alleen adressen die er zijn, en dezelfde als het deployment-endpoint.

Vraag 13 uit de zad-cli-doorloop: project `p1-wan` met een component `worker` zonder poort
en zonder publish-on-web. `POST /:refresh` gaf
`{"worker": "https://worker-productie-p1-wan..."}` terug, `GET /deployments/productie` gaf
`{}`, en het adres zelf gaf 404. Twee endpoints die hetzelfde moment anders beschrijven,
omdat ze het antwoord allebei zelf samenstelden: het deployment-endpoint keek of er een
ingress was, de refresh vormde de naam uit `{component}-{deployment}-{project}`.

Wat deze poort meet is niet de tekst maar de bron. Er is er nu een -- publish-on-web weet of
een component een ingress krijgt -- en alle drie de lezers (refresh, deployment-endpoint,
detailpagina) halen hem daar op. Een adres teruggeven dat 404 geeft is erger dan geen adres:
een client slaat het op en geeft het door.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from opi.api.v2.router import _compute_deployment_urls
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.project_manager import DeploymentResult, ProjectManager
from opi.services.catalog.publish_on_web.urls import public_url_map_for_deployment

CLUSTER = "local"
PROJECT = "p1-wan"
DEPLOYMENT = "productie"


def _project(component_services: list[str]) -> dict[str, Any]:
    """Het project uit de melding: een component, met of zonder publish-on-web."""
    return {
        "name": PROJECT,
        "components": [
            {
                "name": "worker",
                "type": "single",
                "services": list(component_services),
            }
        ],
        "deployments": [
            {
                "name": DEPLOYMENT,
                "cluster": CLUSTER,
                "namespace": PROJECT,
                "components": [{"reference": "worker", "image": "busybox:1.36"}],
            }
        ],
    }


def _deployment(project_data: dict[str, Any]) -> dict[str, Any]:
    return project_data["deployments"][0]


def test_component_zonder_publish_on_web_krijgt_geen_adres() -> None:
    """Geen ingress, dus geen adres -- niet de naam die het adres zou zijn."""
    project_data = _project([])

    urls = public_url_map_for_deployment(project_data, _deployment(project_data), PROJECT, ProjectFileHandler())

    assert urls == {}


def test_component_met_publish_on_web_krijgt_zijn_adres() -> None:
    """De keerzijde: publiceert hij wel, dan staat hij er met zijn hostnaam in."""
    project_data = _project(["publish-on-web"])

    urls = public_url_map_for_deployment(project_data, _deployment(project_data), PROJECT, ProjectFileHandler())

    assert list(urls) == ["worker"]
    assert urls["worker"].endswith(f"//worker-{DEPLOYMENT}-{PROJECT}.kind")


@pytest.mark.parametrize("services", [[], ["publish-on-web"]])
def test_deployment_endpoint_leest_dezelfde_bron(services: list[str]) -> None:
    """Het deployment-endpoint stelt niets meer zelf samen, dus het kan niet afwijken."""
    project_data = _project(services)
    deployment = _deployment(project_data)

    assert _compute_deployment_urls(deployment, PROJECT, project_data) == public_url_map_for_deployment(
        project_data, deployment, PROJECT, ProjectFileHandler()
    )


@pytest.mark.parametrize(
    ("services", "verwacht"),
    [([], set()), (["publish-on-web"], {"worker"})],
)
def test_refresh_rapporteert_wat_publish_on_web_zegt(services: list[str], verwacht: set[str]) -> None:
    """Wat de refresh teruggeeft komt uit die ene bron.

    De manager vulde dit tot RC-104 per component in de manifestlus, uit de naamgeving en
    zonder te kijken of de ingress er ook kwam; dat is wat `p1-wan` een dood adres opleverde.
    Aangeroepen op een stand-in `self`, want de omliggende methode kloont een git-repo.
    """
    project_data = _project(services)
    manager = SimpleNamespace(
        _deployment_results={
            DEPLOYMENT: DeploymentResult(deployment_name=DEPLOYMENT, cluster=CLUSTER, namespace=PROJECT)
        },
        _project_file_handler=ProjectFileHandler(),
    )

    ProjectManager._track_public_urls(manager, project_data, _deployment(project_data), PROJECT)  # type: ignore[arg-type]

    assert set(manager._deployment_results[DEPLOYMENT].urls) == verwacht
