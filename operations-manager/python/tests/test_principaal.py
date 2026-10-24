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
from opi.api import principaal as mod
from opi.api.principaal import (
    TOKENSOORT_NIET_TOEGESTAAN,
    NietHerkend,
    PrincipaalSoort,
    ToegangGeweigerd,
    beslis,
    herken_principaal,
    hoort_bij_project,
    reset_gebruik,
    tel_gebruik,
)
from opi.core.handelingen import Handeling
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
    principaal = await herken_principaal(TOKEN_BEHEERDER, project)
    assert principaal.soort is PrincipaalSoort.PERSOON
    assert principaal.naam == BEHEERDER
    assert str(principaal.actor) == BEHEERDER


@pytest.mark.asyncio
async def test_een_token_van_een_service_account_draagt_zijn_naam(project: ProjectSummary) -> None:
    """De naam is het punt van een service account: zonder naam valt er niets te loggen."""
    principaal = await herken_principaal(TOKEN_BOUWSTRAAT, project)
    assert principaal.soort is PrincipaalSoort.SERVICE_ACCOUNT
    assert principaal.naam == "bouwstraat"
    assert str(principaal.actor) == "service-account:bouwput/bouwstraat"


@pytest.mark.asyncio
async def test_een_onbekend_token_wordt_niet_herkend(project: ProjectSummary) -> None:
    with pytest.raises(NietHerkend):
        await herken_principaal("zad_nooit-uitgegeven", project)


@pytest.mark.asyncio
async def test_een_verlopen_token_opent_niets(project: ProjectSummary) -> None:
    """Zonder dat iemand iets intrekt. Dat is waar de verplichte vervaldatum voor is."""
    with pytest.raises(NietHerkend):
        await herken_principaal(TOKEN_VERLOPEN, project)


@pytest.mark.asyncio
async def test_een_token_van_een_ander_project_geeft_dezelfde_401(project: ProjectSummary) -> None:
    """Niet een 403: anders is het verschil een manier om projectnamen af te tasten."""
    ander = _project("anderproject")
    ander.users = [ProjectUser(email="iemand@rijksoverheid.nl", role="admin")]
    with pytest.raises(NietHerkend):
        await herken_principaal(TOKEN_BEHEERDER, ander)


@pytest.mark.asyncio
async def test_een_mens_zonder_platformtoegang_verliest_zijn_token(
    project: ProjectSummary, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offboarding in het gebruikersbeheer raakt geen projectbestand, maar wel dit token."""
    monkeypatch.setattr(get_user_service(), "is_email_allowed", lambda email: False)
    with pytest.raises(NietHerkend):
        await herken_principaal(TOKEN_BEHEERDER, project)


@pytest.mark.asyncio
async def test_de_oude_projectsleutel_werkt_zolang_hij_in_het_bestand_staat() -> None:
    """Alleen voor een bestand waarvan de migratie naar 2.12 de sleutel niet open kreeg."""
    project = _project()
    project.api_key = "oude-sleutel"
    principaal = await herken_principaal("oude-sleutel", project)
    assert principaal.soort is PrincipaalSoort.PROJECTSLEUTEL
    assert str(principaal.actor) == "project-api-key:bouwput"


# --- beslissen -------------------------------------------------------------


@pytest.mark.asyncio
async def test_een_beheerder_mag_schrijven(project: ProjectSummary) -> None:
    principaal = await herken_principaal(TOKEN_BEHEERDER, project)
    beslis(principaal, project=project, handeling=Handeling.DEPLOYMENT_SCHRIJVEN)


@pytest.mark.asyncio
async def test_een_developer_leest_wel_en_schrijft_niet(project: ProjectSummary) -> None:
    principaal = await herken_principaal(TOKEN_DEV, project)
    beslis(principaal, project=project, handeling=Handeling.DEPLOYMENT_LEZEN)
    with pytest.raises(ToegangGeweigerd):
        beslis(principaal, project=project, handeling=Handeling.DEPLOYMENT_SCHRIJVEN)


@pytest.mark.asyncio
async def test_een_token_sterft_met_het_lidmaatschap(project: ProjectSummary) -> None:
    """De eis uit het plan: wie uit ``users`` gaat verliest zijn token, zonder intrekken.

    De rol wordt bij het BESLISSEN opnieuw gelezen, dus een bestand dat tussen herkennen en
    beslissen verandert telt al mee.
    """
    principaal = await herken_principaal(TOKEN_BEHEERDER, project)
    project.users = [lid for lid in project.users or [] if lid.email != BEHEERDER]

    with pytest.raises(ToegangGeweigerd, match="geen lid"):
        beslis(principaal, project=project, handeling=Handeling.PROJECT_LEZEN)


@pytest.mark.asyncio
async def test_een_verlaagde_rol_werkt_meteen_door(project: ProjectSummary) -> None:
    """Een token draagt een VERWIJZING naar zijn lid, geen kopie van diens rechten."""
    principaal = await herken_principaal(TOKEN_BEHEERDER, project)
    for lid in project.users or []:
        if lid.email == BEHEERDER:
            lid.role = "developer"

    with pytest.raises(ToegangGeweigerd, match="developer"):
        beslis(principaal, project=project, handeling=Handeling.DEPLOYMENT_SCHRIJVEN)


@pytest.mark.asyncio
async def test_een_service_account_mag_geen_tokens_beheren(project: ProjectSummary) -> None:
    """Een pijplijn hoort zijn eigen bewijsmiddel niet te kunnen vermenigvuldigen.

    Hij is admin en heeft dus de rol ervoor; wat hem tegenhoudt is zijn SOORT, en dat
    zegt de cause ook.
    """
    principaal = await herken_principaal(TOKEN_BOUWSTRAAT, project)
    beslis(principaal, project=project, handeling=Handeling.DEPLOYMENT_SCHRIJVEN)

    with pytest.raises(ToegangGeweigerd) as fout:
        beslis(principaal, project=project, handeling=Handeling.TOKEN_BEHEREN)
    assert fout.value.cause == TOKENSOORT_NIET_TOEGESTAAN


@pytest.mark.asyncio
async def test_een_mens_mag_wel_tokens_beheren(project: ProjectSummary) -> None:
    principaal = await herken_principaal(TOKEN_BEHEERDER, project)
    beslis(principaal, project=project, handeling=Handeling.TOKEN_BEHEREN)


@pytest.mark.asyncio
async def test_de_oude_sleutel_mag_geen_tokens_beheren() -> None:
    project = _project()
    project.api_key = "oude-sleutel"
    principaal = await herken_principaal("oude-sleutel", project)
    with pytest.raises(ToegangGeweigerd):
        beslis(principaal, project=project, handeling=Handeling.TOKEN_BEHEREN)


# --- voorcontrole van de image-push ----------------------------------------


@pytest.mark.asyncio
async def test_hoort_bij_project_valt_op_een_verdwenen_lid(project: ProjectSummary) -> None:
    principaal = await herken_principaal(TOKEN_BOUWSTRAAT, project)
    assert hoort_bij_project(principaal, project)

    project.users = [lid for lid in project.users or [] if lid.name != "bouwstraat"]
    assert not hoort_bij_project(principaal, project)


# --- meting ----------------------------------------------------------------


def test_het_gebruik_wordt_per_project_en_soort_geteld() -> None:
    """Zonder deze telling is het afbouwpad van de projectsleutel een wens."""
    reset_gebruik()
    tel_gebruik("bouwput", PrincipaalSoort.PROJECTSLEUTEL)
    tel_gebruik("bouwput", PrincipaalSoort.PROJECTSLEUTEL)
    tel_gebruik("bouwput", PrincipaalSoort.SERVICE_ACCOUNT)

    assert mod.gebruik_per_project() == {
        ("bouwput", "projectsleutel"): 2,
        ("bouwput", "service-account"): 1,
    }
    reset_gebruik()
