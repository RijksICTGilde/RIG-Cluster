"""De ingang zet de actor en telt het bewijsmiddel, en weigert met de goede code.

De actor wordt SERVER-SIDE gezet, op de ingang die de aanroeper herkend heeft, en nooit uit
een body. Daardoor is in het logboek en in de git-trailer te zien of een wijziging van een
mens kwam, van een benoemd service account, of nog van de oude projectsleutel
(``project-api-key:<project>``).

Wat hier wordt vastgelegd is de WAARDE die de trailer uit RC-284 draagt; de vorm ervan
staat in ``opi/core/actor.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from opi.api.endpoint_util import REQUIRES_PROJECT_NAME, handhaaf
from opi.api.principal import PRINCIPAL_KIND_NOT_ALLOWED, PrincipalKind, gebruik_per_project, reset_gebruik
from opi.core.actor import ACTOR_UNKNOWN, get_actor
from opi.core.authz.actions import Action, action
from opi.core.leden import LidSoort
from opi.services.project_leden import hash_van
from opi.services.project_service import ProjectSummary, ProjectToken, ProjectUser
from opi.services.user_service import get_user_service
from starlette.requests import Request

pytestmark = pytest.mark.asyncio

BEHEERDER = "beheerder@rijksoverheid.nl"
TOKEN_MENS = "zad_mens"
TOKEN_BOUWSTRAAT = "zad_bouwstraat"


def _token(naam: str, waarde: str) -> ProjectToken:
    return ProjectToken(name=naam, expires=datetime.now(UTC) + timedelta(days=30), hash=hash_van(waarde))


def _project(*, met_oude_sleutel: bool = False, beheerder: str = BEHEERDER) -> ProjectSummary:
    return ProjectSummary(
        name="bouwput",
        api_key="de-oude-sleutel" if met_oude_sleutel else None,
        filename="projects/bouwput.yaml",
        users=[
            ProjectUser(email=beheerder, role="admin", tokens=[_token("cli", TOKEN_MENS)]),
            ProjectUser(
                type=LidSoort.SERVICE_ACCOUNT,
                name="bouwstraat",
                role="admin",
                tokens=[_token("pijplijn", TOKEN_BOUWSTRAAT)],
            ),
        ],
    )


@pytest.fixture(autouse=True)
def _schone_meting():
    reset_gebruik()
    yield
    reset_gebruik()


@pytest.fixture
def platformgebruiker():
    """Het adres staat in de platform-inlooglijst, zoals bij een mens die nog in dienst is."""
    dienst = get_user_service()
    dienst.add_allowed_email(BEHEERDER)
    yield
    dienst.remove_allowed_email(BEHEERDER)


def _verzoek() -> Request:
    """Een echt ``Request``: ``handhaaf`` legt de eigenaar van het token op ``state`` en
    telt een mislukte poging af in de rate limiter, en beide raken het verzoek."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/projects/bouwput",
            "headers": [],
            "client": ("10.0.0.1", 1234),
        }
    )


def _route(welke: Action):
    async def endpoint(project_name: str) -> None: ...

    setattr(endpoint, REQUIRES_PROJECT_NAME, True)
    return action(welke)(endpoint)


async def test_de_oude_sleutel_zet_de_actor_en_telt_als_oud() -> None:
    """De telling is wat het afbouwpad meetbaar maakt: dit was de OUDE weg."""
    await handhaaf(_verzoek(), "de-oude-sleutel", _route(Action.PROJECT_READ), _project(met_oude_sleutel=True))

    assert str(get_actor()) == "project-api-key:bouwput"
    assert gebruik_per_project() == {("bouwput", PrincipalKind.PROJECT_KEY.value): 1}


async def test_een_verkeerde_sleutel_is_401_en_zet_geen_actor() -> None:
    with pytest.raises(HTTPException) as fout:
        await handhaaf(_verzoek(), "niet-de-sleutel", _route(Action.PROJECT_READ), _project(met_oude_sleutel=True))

    assert fout.value.status_code == 401
    assert get_actor() == ACTOR_UNKNOWN
    assert gebruik_per_project() == {}


async def test_een_route_zonder_handeling_laat_niets_door() -> None:
    """De opstartcontrole hoort dit onmogelijk te maken; komt het er toch langs, dan is
    stilzwijgend doorlaten de verkeerde uitkomst."""

    async def endpoint(project_name: str) -> None: ...

    with pytest.raises(RuntimeError, match="draagt geen handeling"):
        await handhaaf(_verzoek(), "de-oude-sleutel", endpoint, _project(met_oude_sleutel=True))


async def test_een_servicaccounttoken_zet_zijn_eigen_actor() -> None:
    await handhaaf(_verzoek(), TOKEN_BOUWSTRAAT, _route(Action.TASK_READ), _project())

    assert str(get_actor()) == "service-account:bouwput/bouwstraat"
    assert gebruik_per_project() == {("bouwput", PrincipalKind.SERVICE_ACCOUNT.value): 1}


async def test_een_token_van_een_mens_zet_zijn_e_mailadres_als_actor(platformgebruiker) -> None:
    await handhaaf(_verzoek(), TOKEN_MENS, _route(Action.PROJECT_READ), _project())

    assert str(get_actor()) == BEHEERDER
    assert gebruik_per_project() == {("bouwput", PrincipalKind.HUMAN.value): 1}


async def test_een_onbekend_token_is_401_en_zet_geen_actor() -> None:
    with pytest.raises(HTTPException) as fout:
        await handhaaf(_verzoek(), "zad_nooit-uitgegeven", _route(Action.PROJECT_READ), _project())

    assert fout.value.status_code == 401
    assert get_actor() == ACTOR_UNKNOWN
    assert gebruik_per_project() == {}


async def test_een_token_van_iemand_zonder_platformtoegang_is_401() -> None:
    """Een token is niet meer dan de persoon: wie in het gebruikersbeheer is verwijderd
    heeft geen platformtoegang meer, en zijn token dan ook niet. Dat beheer raakt geen
    projectbestand, dus zonder deze lezing bleef het token tot zijn vervaldatum geldig."""
    project = _project(beheerder="vertrokken@rijksoverheid.nl")

    with pytest.raises(HTTPException) as fout:
        await handhaaf(_verzoek(), TOKEN_MENS, _route(Action.PROJECT_DELETE), project)

    assert fout.value.status_code == 401
    assert get_actor() == ACTOR_UNKNOWN
    assert gebruik_per_project() == {}


async def test_een_service_account_op_een_mensenhandeling_ketst_af_op_de_soort() -> None:
    """De reden is de SOORT aanroeper en niet zijn rol; die klopt.

    Sinds RC-392 vraagt ``token.manage`` een verse step-up in plaats van onvoorwaardelijk
    een mens, dus het ANTWOORD komt van ``@requires_step_up`` (een problem-detail met de
    weg eruit) en niet meer als kale 403 uit ``handhaaf``. Wat hier vast moet staan is dat
    de beslissing zelf de soort aanwijst; het antwoord dekt ``tests/test_token_step_up.py``.
    """
    verzoek = _verzoek()
    await handhaaf(verzoek, TOKEN_BOUWSTRAAT, _route(Action.TOKEN_MANAGE), _project())

    beslissing = verzoek.state.authz_decision
    assert beslissing.allowed is False
    assert beslissing.cause.value == PRINCIPAL_KIND_NOT_ALLOWED
    assert beslissing.step_up_would_help is True


async def test_een_token_van_een_ander_project_is_401() -> None:
    """Dezelfde 401 als een onbekend project: anders is het verschil tussen de twee
    antwoorden een manier om projectnamen af te tasten."""
    ander = ProjectSummary(
        name="anderproject",
        filename="projects/anderproject.yaml",
        users=[ProjectUser(email="iemand@rijksoverheid.nl", role="admin")],
    )

    with pytest.raises(HTTPException) as fout:
        await handhaaf(_verzoek(), TOKEN_MENS, _route(Action.PROJECT_READ), ander)

    assert fout.value.status_code == 401
    assert fout.value.detail == "Invalid API key"
