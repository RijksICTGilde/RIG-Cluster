"""Het handhavingspunt: wat een bewijsmiddel opent, en waar het ophoudt.

De eigenschappen die hier worden vastgelegd zijn precies die waarom dit bestaat:

- een token van project A wordt in project B niet eens herkend, en krijgt daar dezelfde
  401 als een onbekend token, zodat projectnamen niet aftastbaar zijn;
- een verlopen token opent niets, zonder dat iemand iets hoeft in te trekken;
- een token is nooit meer dan zijn lid NU is: wie uit ``users`` verdwijnt of een lagere
  rol krijgt verliest dat dezelfde seconde;
- een service account komt nooit langs een handeling die een geverifieerde mens vraagt.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from opi.api import principal as mod
from opi.api.principal import (
    AccessDenied,
    NotRecognised,
    PrincipalKind,
    beslis,
    recognise_principal,
    reset_gebruik,
    tel_gebruik,
)
from opi.core.authz.actions import Action
from opi.core.leden import LidSoort
from opi.services.project_leden import hash_van
from opi.services.project_service import ProjectSummary, ProjectToken, ProjectUser
from opi.services.user_service import get_user_service

BEHEERDER = "beheerder@rijksoverheid.nl"
DEV = "dev@rijksoverheid.nl"

TOKEN_BEHEERDER = "zad_beheerder"
TOKEN_DEV = "zad_dev"
TOKEN_BOUWSTRAAT = "zad_bouwstraat"
TOKEN_VERLOPEN = "zad_verlopen"


def _token(naam: str, waarde: str, *, dagen: int = 30) -> ProjectToken:
    return ProjectToken(name=naam, expires=datetime.now(UTC) + timedelta(days=dagen), hash=hash_van(waarde))


def _project(naam: str = "bouwput") -> ProjectSummary:
    """Een project met een beheerder, een ontwikkelaar en een service account."""
    return ProjectSummary(
        name=naam,
        api_key=None,
        filename=f"projects/{naam}.yaml",
        users=[
            ProjectUser(
                email=BEHEERDER,
                role="admin",
                tokens=[_token("cli", TOKEN_BEHEERDER), _token("oud", TOKEN_VERLOPEN, dagen=-1)],
            ),
            ProjectUser(email=DEV, role="developer", tokens=[_token("cli", TOKEN_DEV)]),
            ProjectUser(
                type=LidSoort.SERVICE_ACCOUNT,
                name="bouwstraat",
                role="admin",
                tokens=[_token("pijplijn", TOKEN_BOUWSTRAAT)],
            ),
        ],
    )


@pytest.fixture
def project() -> ProjectSummary:
    return _project()


@pytest.fixture(autouse=True)
def _iedereen_mag_het_platform_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """De platformtoegang is een eigen controle; hier gaat het om het projectbestand."""
    monkeypatch.setattr(get_user_service(), "is_email_allowed", lambda email: True)


# --- herkennen -------------------------------------------------------------


@pytest.mark.asyncio
async def test_een_token_van_een_mens_wordt_die_mens(project: ProjectSummary) -> None:
    principal = await recognise_principal(TOKEN_BEHEERDER, project)
    assert principal.kind is PrincipalKind.HUMAN
    assert principal.name == BEHEERDER
    assert str(principal.actor) == BEHEERDER


@pytest.mark.asyncio
async def test_een_token_van_een_service_account_draagt_zijn_naam(project: ProjectSummary) -> None:
    """De naam is het punt van een service account: zonder naam valt er niets te loggen."""
    principal = await recognise_principal(TOKEN_BOUWSTRAAT, project)
    assert principal.kind is PrincipalKind.SERVICE_ACCOUNT
    assert principal.name == "bouwstraat"
    assert str(principal.actor) == "service-account:bouwput/bouwstraat"


@pytest.mark.asyncio
async def test_een_onbekend_token_wordt_niet_herkend(project: ProjectSummary) -> None:
    with pytest.raises(NotRecognised):
        await recognise_principal("zad_nooit-uitgegeven", project)


@pytest.mark.asyncio
async def test_een_verlopen_token_opent_niets(project: ProjectSummary) -> None:
    """Zonder dat iemand iets intrekt. Dat is waar de verplichte vervaldatum voor is."""
    with pytest.raises(NotRecognised):
        await recognise_principal(TOKEN_VERLOPEN, project)


@pytest.mark.asyncio
async def test_een_token_van_een_ander_project_geeft_dezelfde_401(project: ProjectSummary) -> None:
    """Niet een 403: anders is het verschil een manier om projectnamen af te tasten."""
    ander = _project("anderproject")
    ander.users = [ProjectUser(email="iemand@rijksoverheid.nl", role="admin")]
    with pytest.raises(NotRecognised):
        await recognise_principal(TOKEN_BEHEERDER, ander)


@pytest.mark.asyncio
async def test_een_mens_zonder_platformtoegang_verliest_zijn_token(
    project: ProjectSummary, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offboarding in het gebruikersbeheer raakt geen projectbestand, maar wel dit token."""
    monkeypatch.setattr(get_user_service(), "is_email_allowed", lambda email: False)
    with pytest.raises(NotRecognised):
        await recognise_principal(TOKEN_BEHEERDER, project)


@pytest.mark.asyncio
async def test_de_oude_projectsleutel_werkt_zolang_hij_in_het_bestand_staat() -> None:
    """Alleen voor een bestand waarvan de migratie naar 2.12 de sleutel niet open kreeg."""
    project = _project()
    project.api_key = "oude-sleutel"
    principal = await recognise_principal("oude-sleutel", project)
    assert principal.kind is PrincipalKind.PROJECT_KEY
    assert str(principal.actor) == "project-api-key:bouwput"


# --- beslissen -------------------------------------------------------------


@pytest.mark.asyncio
async def test_een_beheerder_mag_schrijven(project: ProjectSummary) -> None:
    principal = await recognise_principal(TOKEN_BEHEERDER, project)
    beslis(principal, project=project, handeling=Action.DEPLOYMENT_WRITE)


@pytest.mark.asyncio
async def test_een_developer_schrijft_buiten_productie_wel_en_op_productie_niet(
    project: ProjectSummary,
) -> None:
    """Sinds RC-392 weegt de poort de MARKERING en niet alleen de rol (ontwerp rij 2 en 3)."""
    from opi.core.authz.engine import Resource

    principal = await recognise_principal(TOKEN_DEV, project)
    beslis(principal, project=project, handeling=Action.DEPLOYMENT_READ)

    acceptatie = Resource(project=project.name, path="project/x/deployment/acc", deployment="acc")
    beslis(principal, project=project, handeling=Action.DEPLOYMENT_WRITE, resource=acceptatie)

    productie = Resource(
        project=project.name, path="project/x/deployment/live", deployment="live", environment="productie"
    )
    with pytest.raises(AccessDenied):
        beslis(principal, project=project, handeling=Action.DEPLOYMENT_WRITE, resource=productie)


@pytest.mark.asyncio
async def test_een_token_sterft_met_het_lidmaatschap(project: ProjectSummary) -> None:
    """De eis uit het plan: wie uit ``users`` gaat verliest zijn token, zonder intrekken.

    De rol wordt bij het BESLISSEN opnieuw gelezen, dus een bestand dat tussen herkennen en
    beslissen verandert telt al mee.
    """
    principal = await recognise_principal(TOKEN_BEHEERDER, project)
    project.users = [lid for lid in project.users or [] if lid.email != BEHEERDER]

    with pytest.raises(AccessDenied, match="geen lid"):
        beslis(principal, project=project, handeling=Action.PROJECT_READ)


@pytest.mark.asyncio
async def test_een_verlaagde_rol_werkt_meteen_door(project: ProjectSummary) -> None:
    """Een token draagt een VERWIJZING naar zijn lid, geen kopie van diens rechten."""
    from opi.core.authz.engine import Resource

    principal = await recognise_principal(TOKEN_BEHEERDER, project)
    for lid in project.users or []:
        if lid.email == BEHEERDER:
            lid.roles = ["developer"]

    productie = Resource(
        project=project.name, path="project/x/deployment/live", deployment="live", environment="productie"
    )
    with pytest.raises(AccessDenied, match="openen"):
        beslis(principal, project=project, handeling=Action.DEPLOYMENT_WRITE, resource=productie)


@pytest.mark.asyncio
async def test_een_service_account_mag_geen_tokens_beheren(project: ProjectSummary) -> None:
    """Een pijplijn hoort zijn eigen bewijsmiddel niet te kunnen vermenigvuldigen.

    Hij is admin en heeft dus de rol ervoor; wat hem tegenhoudt is zijn SOORT. Sinds
    RC-392 vraagt ``token.manage`` een verse step-up, dus het ANTWOORD komt van
    ``@requires_step_up``; de beslissing hieronder wijst de soort aan.
    """
    from opi.core.authz.engine import Cause

    principal = await recognise_principal(TOKEN_BOUWSTRAAT, project)
    beslis(principal, project=project, handeling=Action.DEPLOYMENT_WRITE)

    beslissing = beslis(principal, project=project, handeling=Action.TOKEN_MANAGE, step_up_fresh=False)
    assert beslissing.allowed is False
    assert beslissing.cause is Cause.PRINCIPAL_KIND_NOT_ALLOWED


@pytest.mark.asyncio
async def test_een_mens_mag_wel_tokens_beheren(project: ProjectSummary) -> None:
    principal = await recognise_principal(TOKEN_BEHEERDER, project)
    beslissing = beslis(principal, project=project, handeling=Action.TOKEN_MANAGE, step_up_fresh=True)
    assert beslissing.allowed is True


@pytest.mark.asyncio
async def test_de_oude_sleutel_mag_geen_tokens_beheren() -> None:
    """Ook hier wijst de beslissing de SOORT aan; de step-up geeft het antwoord."""
    from opi.core.authz.engine import Cause

    project = _project()
    project.api_key = "oude-sleutel"
    principal = await recognise_principal("oude-sleutel", project)
    beslissing = beslis(principal, project=project, handeling=Action.TOKEN_MANAGE, step_up_fresh=False)
    assert beslissing.allowed is False
    assert beslissing.cause is Cause.PRINCIPAL_KIND_NOT_ALLOWED


# --- meting ----------------------------------------------------------------


def test_het_gebruik_wordt_per_project_en_soort_geteld() -> None:
    """Zonder deze telling is het afbouwpad van de projectsleutel een wens."""
    reset_gebruik()
    tel_gebruik("bouwput", PrincipalKind.PROJECT_KEY)
    tel_gebruik("bouwput", PrincipalKind.PROJECT_KEY)
    tel_gebruik("bouwput", PrincipalKind.SERVICE_ACCOUNT)

    assert mod.gebruik_per_project() == {
        ("bouwput", "project-key"): 2,
        ("bouwput", "service-account"): 1,
    }
    reset_gebruik()
