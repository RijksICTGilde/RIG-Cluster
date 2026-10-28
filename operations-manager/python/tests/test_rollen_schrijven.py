"""De schrijfwegen voor rollen, en wat ze weigeren (ontwerp paragraaf 7).

Drie wegen schrijven rollen: de Team-kaart, de API-route en de team-wizard. Ze lopen alle
drie langs één servicefunctie, zodat ze niet uit elkaar kunnen lopen op wat een geldige
rol is of op de eis dat er een admin overblijft. Deze tests pinnen dat vast, van beide
kanten: wie het niet mag krijgt 403 en het bestand verandert niet, en wie het wel mag maar
geen verse step-up heeft krijgt de dialoog in plaats van de wijziging.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.authz.actions import Action
from opi.core.authz.engine import ROLE_LABELS, Role
from opi.core.leden import LidSoort
from opi.forms.wizard.leden import LedenlijstGeweigerd, bewaak_ledenlijst
from opi.services.api_token_service import (
    STANDAARD_ROLLEN,
    TokenAanvraagGeweigerd,
    schoon_rollen,
    zet_rollen_in_data,
)
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web.authz import authorize, may

PROJECT = "bouwput"
BEHEERDER = "beheerder@rijksoverheid.nl"
DEV = "dev@rijksoverheid.nl"


def _project() -> ProjectSummary:
    return ProjectSummary(
        name=PROJECT,
        filename=f"{PROJECT}.yaml",
        data={"name": PROJECT, "deployments": [{"name": "live", "environment": "productie"}]},
        users=[
            ProjectUser(email=BEHEERDER, roles=["admin", "ops"], type=LidSoort.PERSOON),
            ProjectUser(email=DEV, roles=["developer"], type=LidSoort.PERSOON),
        ],
    )


def _verzoek(email: str, *, verse_step_up: bool = False) -> Any:
    sessie: dict[str, Any] = {}
    if verse_step_up:
        from datetime import UTC, datetime

        sessie["step_up_at"] = datetime.now(UTC).isoformat()
    return SimpleNamespace(state=SimpleNamespace(user={"email": email}), session=sessie, path_params={})


def _patches() -> tuple[Any, Any]:
    store = MagicMock()
    store.get.return_value = _project()
    gebruikers = MagicMock()
    gebruikers.is_platform_admin.return_value = False
    return (
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.authz.get_user_service", return_value=gebruikers),
    )


# --- de rolpoort ------------------------------------------------------------


def test_een_developer_mag_de_leden_niet_beheren() -> None:
    """403, en de route komt niet aan het bestand toe."""
    store_patch, user_patch = _patches()
    with store_patch, user_patch, pytest.raises(HTTPException) as fout:
        authorize(_verzoek(DEV), action=Action.MEMBER_MANAGE, project_name=PROJECT)
    assert fout.value.status_code == 403


def test_een_admin_zonder_verse_step_up_komt_niet_langs_de_beslissing() -> None:
    """De rol volstaat, de tweede factor niet: dat is step-up-required, geen weigering."""
    from opi.core.authz.engine import Cause

    store_patch, user_patch = _patches()
    with store_patch, user_patch:
        authorize(_verzoek(BEHEERDER), action=Action.MEMBER_MANAGE, project_name=PROJECT)
    verzoek = _verzoek(BEHEERDER)
    with store_patch, user_patch:
        authorize(verzoek, action=Action.MEMBER_MANAGE, project_name=PROJECT)
    assert verzoek.state.authz_decision.cause is Cause.STEP_UP_REQUIRED
    assert verzoek.state.authz_decision.allowed is False


def test_een_admin_met_een_verse_step_up_mag_het() -> None:
    store_patch, user_patch = _patches()
    verzoek = _verzoek(BEHEERDER, verse_step_up=True)
    with store_patch, user_patch:
        authorize(verzoek, action=Action.MEMBER_MANAGE, project_name=PROJECT)
    assert verzoek.state.authz_decision.allowed is True


def test_een_developer_zet_de_markering_van_een_productiedeployment_niet() -> None:
    """Zetten en verlagen vragen ops met step-up (ontwerp paragraaf 4, rij 16)."""
    store_patch, user_patch = _patches()
    with store_patch, user_patch:
        assert may(_verzoek(DEV), action=Action.ENVIRONMENT_SET, project_name=PROJECT, deployment="live") is False
        assert may(_verzoek(BEHEERDER), action=Action.ENVIRONMENT_SET, project_name=PROJECT, deployment="live") is True


# --- wat de servicefunctie weigert ------------------------------------------


def test_een_lege_lijst_rollen_wordt_geweigerd() -> None:
    with pytest.raises(TokenAanvraagGeweigerd, match="minstens een rol"):
        schoon_rollen([])


def test_een_onbekende_rol_wordt_geweigerd() -> None:
    with pytest.raises(TokenAanvraagGeweigerd, match="Onbekende rol"):
        schoon_rollen(["operator"])


def test_de_rollen_worden_ontdubbeld_en_gesorteerd() -> None:
    assert schoon_rollen(["ops", "admin", "ops"]) == ["admin", "ops"]


def test_de_laatste_admin_weghalen_wordt_geweigerd() -> None:
    data = {
        "users": [
            {"email": BEHEERDER, "roles": ["admin", "ops"]},
            {"email": DEV, "roles": ["developer"]},
        ]
    }
    with pytest.raises(TokenAanvraagGeweigerd, match="minstens een lid met de rol admin"):
        zet_rollen_in_data(data, BEHEERDER, ["developer"])
    assert data["users"][0]["roles"] == ["admin", "ops"], "de lijst blijft staan zoals hij was"


def test_een_tweede_admin_maakt_degraderen_wel_mogelijk() -> None:
    data = {
        "users": [
            {"email": BEHEERDER, "roles": ["admin", "ops"]},
            {"email": DEV, "roles": ["admin"]},
        ]
    }
    assert zet_rollen_in_data(data, BEHEERDER, ["developer"]) is data
    assert data["users"][0]["roles"] == ["developer"]


def test_een_onbestaand_lid_wordt_geweigerd() -> None:
    with pytest.raises(TokenAanvraagGeweigerd, match="geen lid"):
        zet_rollen_in_data({"users": []}, "niemand", ["admin"])


# --- de wizardweg -----------------------------------------------------------


def test_een_flow_die_users_niet_bezit_mag_hem_niet_wijzigen() -> None:
    """Een inzending die ``users`` meebrengt zonder het formulier ervoor te renderen."""
    opgeslagen = {"users": [{"email": BEHEERDER, "roles": ["admin"]}]}
    ingediend = {"users": [{"email": BEHEERDER, "roles": ["admin"]}, {"email": DEV, "roles": ["admin"]}]}
    with pytest.raises(LedenlijstGeweigerd, match="bewerkt de projectleden niet"):
        bewaak_ledenlijst(ingediend, opgeslagen, bezit_users=False)


def test_een_flow_die_users_niet_bezit_mag_hem_ongewijzigd_meesturen() -> None:
    """De sessie van de bewerkwizard draagt het hele project; gelijk is niet gewijzigd."""
    opgeslagen = {"users": [{"email": BEHEERDER, "roles": ["admin"]}]}
    bewaak_ledenlijst({"users": [{"email": BEHEERDER, "roles": ["admin"]}]}, opgeslagen, bezit_users=False)


@pytest.mark.parametrize(
    ("leden", "melding"),
    [
        ([{"email": BEHEERDER, "roles": ["operator"]}], "Onbekende rol"),
        ([{"email": BEHEERDER, "roles": []}], "minstens een rol"),
        ([{"email": DEV, "roles": ["developer"]}], "minstens een lid met de rol admin"),
        ([], "minstens een lid"),
    ],
)
def test_de_wizardweg_weigert_hetzelfde_als_de_servicefunctie(leden: list[Any], melding: str) -> None:
    with pytest.raises(LedenlijstGeweigerd, match=melding):
        bewaak_ledenlijst({"users": leden}, {"users": []}, bezit_users=True)


# --- het label is los van de sleutel (B13) ----------------------------------


def test_een_ander_label_verandert_het_scherm_en_niet_het_bestand() -> None:
    """Hernoemen is een regel in ROLE_LABELS, geen migratie van projectbestanden."""
    from opi.core.authz.matrix import COLUMNS, columns
    from opi.forms.visualizers.providers import UserRoleOptionsProvider

    origineel = ROLE_LABELS[Role.DEVELOPER]
    try:
        ROLE_LABELS[Role.DEVELOPER] = "Bouwer"
        lid = ProjectUser(email=DEV, roles=["developer"], type=LidSoort.PERSOON)
        assert lid.rol_labels == ["Bouwer"], "de Team-kaart toont het label"
        assert lid.roles == ["developer"], "het projectbestand houdt de sleutel"

        opties = {optie["value"]: optie["label"] for optie in UserRoleOptionsProvider().get_options()}
        assert opties["developer"] == "Bouwer", "de wizard toont het label"

        koppen = {kolom.role: kolom.label for kolom in columns()}
        assert koppen[Role.DEVELOPER] == "Bouwer", "de matrix toont het label"
        assert Role.DEVELOPER in COLUMNS
    finally:
        ROLE_LABELS[Role.DEVELOPER] = origineel


def test_een_nieuw_lid_heeft_developer_vooraf_gekozen() -> None:
    """B12: een voorinvulling, geen vaste toekenning."""
    assert list(STANDAARD_ROLLEN) == [Role.DEVELOPER.value]


# --- elke weg leest dezelfde regels ----------------------------------------


def test_de_twee_rolroutes_lopen_langs_set_member_roles() -> None:
    """De API-route en de Team-kaart schrijven geen rollen op eigen houtje.

    Op de BRON en niet op een aanroep: de API-route zit achter een bearerdecorator, en
    die nabouwen zegt niets over waar de rollen geschreven worden. Wat hier vast moet
    staan is dat geen van de twee modules zelf ``roles`` in de projectdata zet.
    """
    import opi.api.token_router as api_router
    import opi.web.router_tokens as web_router
    from opi.services import api_token_service

    for module in (api_router, web_router):
        bron = pathlib.Path(module.__file__ or "").read_text()
        assert "set_member_roles(" in bron, f"{module.__name__} schrijft rollen buiten de servicefunctie om"
        assert '["roles"]' not in bron, f"{module.__name__} zet roles rechtstreeks in de projectdata"

    # De servicefunctie zelf is de enige die het wel doet.
    bron = pathlib.Path(api_token_service.__file__ or "").read_text()
    assert 'lid["roles"] = rollen' in bron


def test_de_wizard_leest_dezelfde_twee_guards() -> None:
    """De team-wizard schrijft de hele lijst in een keer en gaat dus niet per lid langs
    ``set_member_roles``; de twee regels OVER de rollen komen wel uit dezelfde module.

    Zonder dit kan "minstens een admin" of "wat een geldige rol is" opnieuw in de
    formulierlaag landen, en dan lopen de wegen uit elkaar.
    """
    from opi.forms.wizard import leden

    bron = pathlib.Path(leden.__file__ or "").read_text()
    assert "schoon_rollen(" in bron, "de wizard toetst de rollen buiten de servicefunctie om"
    assert "eis_een_admin(" in bron, "de wizard bouwt de admin-eis zelf na"
    assert f'"{Role.ADMIN.value}"' not in bron, "de wizard toetst de rol admin weer zelf"
