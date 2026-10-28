"""De poort van de upsert weegt elk doelwit dat de BODY aanwijst, niet alleen het pad.

Eén body wijst vier dingen aan die ergens anders staan dan de route laat zien: de
deployment die geschreven wordt (``deploymentName``), de deployment waarvan de database
gekopieerd wordt (``cloneFrom``), de kloon die al op de deployment staat en opnieuw gaat
lopen (``forceClone``) en de hostnaam (``domain_format``, ``subdomain``,
``base_domain``). Zonder deze toetsen kon een pijplijntoken met ``developer-ci`` de
productiedatabase naar een eigen deployment klonen en die op de productiehostnaam zetten
(RC-392 securityreview). Klonen vanaf productie is nu ops, ook als de kloon al op de
deployment staat en alleen opnieuw gaat lopen, en de hostnaam van een BESTAANDE
deployment verzetten ook.

De terugval hoort er ook bij: een naam die geen bestaande deployment is weegt tegen de
HOOGSTE markering van het project, nooit tegen de laagste.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.api.endpoint_util import deployment_in_body, handhaaf
from opi.api.router import UpsertDeploymentRequest
from opi.api.v2.router import upsert_deployment_v2
from opi.core.authz.actions import Action, action, deployment_from, deployment_of
from opi.core.leden import LidSoort
from opi.services.project_leden import hash_van
from opi.services.project_roles import build_resource, resource_path
from opi.services.project_service import ProjectSummary, ProjectToken, ProjectUser
from opi.services.user_service import get_user_service
from starlette.requests import Request

TOKEN_CI = "zad_pijplijn"
TOKEN_OPS = "zad_beheer"


def _afgeronde_kloon(bron: str, soort: str = "deployment") -> dict[str, Any]:
    """Een ``clone-from`` dat al een keer gelopen heeft: de normale eindtoestand."""
    return {
        "type": soort,
        "reference": bron,
        "mode": "once",
        "status": {"completed": True, "timestamp": "2026-01-01T00:00:00Z"},
    }


def _project(*, met_productie: bool = True, kloon_op_dev1: dict[str, Any] | None = None) -> ProjectSummary:
    """Een project met twee ontwikkeldeployments en, desgewenst, een productiedeployment."""
    dev1: dict[str, Any] = {"name": "dev1", "environment": "ontwikkel", "components": [{"reference": "web"}]}
    if kloon_op_dev1 is not None:
        dev1["clone-from"] = kloon_op_dev1
    data: dict[str, Any] = {
        "name": "bouwput",
        "deployments": [
            dev1,
            {"name": "dev2", "environment": "ontwikkel", "components": [{"reference": "web"}]},
            {
                "name": "prod1",
                "environment": "productie" if met_productie else "acceptatie",
                "components": [{"reference": "web"}],
            },
        ],
    }
    verloopt = datetime.now(UTC) + timedelta(days=30)
    return ProjectSummary(
        name="bouwput",
        filename="projects/bouwput.yaml",
        users=[
            ProjectUser(
                type=LidSoort.SERVICE_ACCOUNT,
                name="pijplijn",
                roles=["developer-ci"],
                tokens=[ProjectToken(name="ci", expires=verloopt, hash=hash_van(TOKEN_CI))],
            ),
            ProjectUser(
                email="beheer@rijksoverheid.nl",
                roles=["ops"],
                tokens=[ProjectToken(name="cli", expires=verloopt, hash=hash_van(TOKEN_OPS))],
            ),
        ],
        data=data,
    )


@pytest.fixture(autouse=True)
def _iedereen_mag_het_platform_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """De platformtoegang is een eigen controle; hier gaat het om de rollen."""
    monkeypatch.setattr(get_user_service(), "is_email_allowed", lambda email: True)


class _Body:
    """Zo veel van ``UpsertDeploymentRequest`` als de poort leest."""

    def __init__(self, deploymentName: str) -> None:
        self.deploymentName = deploymentName


def _request(token: str = "") -> Request:
    """Een echt verzoek: de poort leest headers, path_params en state."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("test", 80),
            "root_path": "",
            "path": "/api/v2/projects/bouwput/:upsert-deployment",
            "query_string": b"",
            "headers": [(b"x-api-key", token.encode())] if token else [],
            "path_params": {},
            "state": {},
        }
    )


@action(Action.DEPLOYMENT_WRITE)
@deployment_from(deployment_in_body)
async def _route(request: Request, project_name: str, deployment_data: Any) -> None:
    """Een route met dezelfde verklaring als de upsert."""


async def _laat_toe(token: str, deployment_naam: str) -> Request:
    """Draai de routepoort voor dit token op deze deployment, en geef het verzoek terug."""
    project = _project()
    verzoek = _request()
    store = MagicMock()
    store.get.return_value = project
    with patch("opi.api.endpoint_util.get_project_store", return_value=store):
        await handhaaf(
            verzoek,
            token,
            _route,
            project,
            route_kwargs={"project_name": project.name, "deployment_data": _Body(deployment_naam)},
        )
    return verzoek


def test_deployment_in_body_leest_de_naam_uit_het_veld() -> None:
    assert deployment_in_body(deployment_data=_Body("dev1")) == "dev1"
    assert deployment_in_body(deployment_data=_Body("")) is None
    assert deployment_in_body(deployment_data=None) is None


def test_deployment_of_leest_de_verklaring_van_de_route() -> None:
    """Een route zonder verklaring wijst geen deployment aan; met wel."""

    async def kaal(request: Request) -> None: ...

    assert deployment_of(kaal, {}) is None
    assert deployment_of(_route, {"project_name": "bouwput", "deployment_data": _Body("prod1")}) == "prod1"


def test_een_onbekende_naam_valt_terug_op_de_hoogste_markering() -> None:
    """De terugval kan alleen strenger uitvallen, nooit ruimer."""
    project = _project()
    for naam in ("PROD1", "nieuw", None):
        doel = build_resource(
            project,
            project_name=project.name,
            path=resource_path(project.name, naam),
            deployment=naam,
        )
        assert doel.environment == "productie", naam


@pytest.mark.asyncio
async def test_de_pijplijn_mag_zijn_eigen_ontwikkeldeployment_schrijven() -> None:
    await _laat_toe(TOKEN_CI, "dev1")


@pytest.mark.asyncio
async def test_de_pijplijn_mag_de_productiedeployment_niet_schrijven() -> None:
    with pytest.raises(HTTPException) as fout:
        await _laat_toe(TOKEN_CI, "prod1")
    assert fout.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token", "body", "verwacht"),
    [
        (TOKEN_CI, {"deploymentName": "dev1"}, 202),
        (TOKEN_CI, {"deploymentName": "prod1"}, 403),
        (TOKEN_CI, {"deploymentName": "dev1", "cloneFrom": "prod1", "forceClone": True}, 403),
        (TOKEN_CI, {"deploymentName": "dev1", "cloneFrom": "dev2"}, 202),
        (TOKEN_CI, {"deploymentName": "dev1", "subdomain": "wies"}, 403),
        (TOKEN_OPS, {"deploymentName": "dev1", "cloneFrom": "prod1", "forceClone": True}, 202),
        (TOKEN_OPS, {"deploymentName": "dev1", "subdomain": "wies"}, 202),
    ],
)
async def test_de_upsert_weegt_elk_doelwit_in_de_body(
    token: str, body: dict[str, Any], verwacht: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """De hele route, zoals een pijplijn hem aanroept."""
    await _roep_de_upsert_aan(token, body, verwacht, monkeypatch, _project())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "verwacht"),
    [
        ({"deploymentName": "nieuw", "subdomain": "wies"}, 202),
        ({"deploymentName": "dev1", "subdomain": "wies"}, 403),
    ],
)
async def test_het_domeindrietal_weegt_alleen_op_een_bestaande_deployment(
    body: dict[str, Any], verwacht: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Een NIEUWE deployment krijgt zijn hostnaam mee uit deployment.write; verzetten is ops."""
    await _roep_de_upsert_aan(TOKEN_CI, body, verwacht, monkeypatch, _project(met_productie=False))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token", "body", "kloon", "verwacht"),
    [
        # forceClone zonder cloneFrom laat de kloon lopen die AL op dev1 staat: de bron
        # daarvan weegt, want de aanroeper kiest hier het moment en niet de bron.
        (TOKEN_CI, {"deploymentName": "dev1", "forceClone": True}, _afgeronde_kloon("prod1"), 403),
        (TOKEN_CI, {"deploymentName": "dev1", "forceClone": True}, _afgeronde_kloon("dev2"), 202),
        (TOKEN_OPS, {"deploymentName": "dev1", "forceClone": True}, _afgeronde_kloon("prod1"), 202),
        # Zonder forceClone verzet deze aanroep de kloon niet, dus er is niets te wegen.
        (TOKEN_CI, {"deploymentName": "dev1"}, _afgeronde_kloon("prod1"), 202),
        # Niets opgeslagen: forceClone kloont dan nergens vandaan.
        (TOKEN_CI, {"deploymentName": "dev1", "forceClone": True}, None, 202),
        # Een cloneFrom in de body VERVANGT de opgeslagen bron, dus die weegt en de oude niet.
        (TOKEN_CI, {"deploymentName": "dev1", "cloneFrom": "dev2", "forceClone": True}, _afgeronde_kloon("prod1"), 202),
        # Een bron die geen deployment van dit project is weegt op projectniveau, en dus
        # tegen de hoogste markering: in een project met productie is dat ops.
        (TOKEN_CI, {"deploymentName": "dev1", "forceClone": True}, _afgeronde_kloon("nacht", "backup"), 403),
    ],
)
async def test_forceclone_weegt_de_kloon_die_al_op_de_deployment_staat(
    token: str, body: dict[str, Any], kloon: dict[str, Any] | None, verwacht: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """De overslag op "once en afgerond" wijkt voor force_clone, dus dit kloont echt opnieuw."""
    await _roep_de_upsert_aan(token, body, verwacht, monkeypatch, _project(kloon_op_dev1=kloon))


async def _roep_de_upsert_aan(
    token: str, body: dict[str, Any], verwacht: int, monkeypatch: pytest.MonkeyPatch, project: ProjectSummary
) -> None:
    verzoek = _request(token)
    store = MagicMock()
    store.get.return_value = project

    async def _taak(**_: Any) -> dict[str, Any]:
        return {"task_id": "t-1", "status": "pending", "task_type": "upsert_deployment"}

    monkeypatch.setattr("opi.api.v2.router.create_async_task", _taak)
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=store),
        patch("opi.api.v2.router.get_project_store", return_value=store),
    ):
        try:
            antwoord = await upsert_deployment_v2(
                request=verzoek,
                project_name=project.name,
                deployment_data=UpsertDeploymentRequest(components=[], **body),
                rollout=True,
            )
        except HTTPException as fout:
            gekregen, reden = fout.status_code, fout.detail
        else:
            gekregen, reden = antwoord.status_code, antwoord.body

    assert gekregen == verwacht, reden
