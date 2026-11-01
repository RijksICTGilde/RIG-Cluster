"""Elke wijzigende beheerroute in het PORTAAL vraagt een verse OTP (RC-409).

De API-tegenhangers in ``opi/api/admin_router.py`` droegen hem sinds RC-377 wel, de vijf
portaalroutes van gebruikersbeheer niet: een overgenomen beheerderssessie kon een
gebruiker aanmaken, wijzigen, verwijderen, zijn sessies beeindigen en zijn tweede factor
leegmaken zonder code. ``test_platformbeheer_step_up.py`` doet hetzelfde voor
``/api/v2/admin/*``.

Per route twee kanten: zonder verse step-up komt de weigering en raakt de dienst niets,
met een verse step-up loopt de route door. Voor de twee formulierroutes bewijst de test
daarnaast dat de dialoog de ingevulde velden herhaalt, want de dialoog neemt de plaats van
het formulier in.

De weigering rendert de dialoog alleen op een HX-Request en werpt anders een 403. De vijf
formulieren posten daarom met htmx naast hun action, en de pagina's eromheen dragen de
schil waarin die dialoog landt. Dat is wat de sjabloontoetsen onderaan vastleggen: zonder
die twee zou de beheerder een kale 403 krijgen en nergens een veld voor zijn code.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.step_up import STEP_UP_SESSION_KEY, StepUpAction
from opi.web import router_user_admin
from opi.web.router_user_admin import _FORMULIERVELDEN

SJABLONEN = Path(router_user_admin.__file__).parent.parent / "templates_lotc"

BEHEERDER = "beheerder@rijksoverheid.nl"
ANDER = "ander@rijksoverheid.nl"
USER_ID = "11111111-1111-1111-1111-111111111111"
FORMULIER = {"email": ANDER, "full_name": "Ander Iemand", "csrf_token": "t"}


class _Formulier(dict):
    """Wat ``request.form()`` teruggeeft: een mapping met ``getlist``."""

    def getlist(self, naam: str) -> list[str]:
        return [self[naam]] if naam in self else []


def _verzoek(*, verse_step_up: bool, htmx: bool = False, pad: str = "/admin/users/create") -> Any:
    sessie = {STEP_UP_SESSION_KEY: datetime.now(UTC).isoformat()} if verse_step_up else {}
    verzoek = SimpleNamespace(
        state=SimpleNamespace(user={"email": BEHEERDER}),
        session=sessie,
        method="POST",
        url=SimpleNamespace(path=pad),
        headers={"HX-Request": "true"} if htmx else {},
        query_params={},
    )
    verzoek.form = AsyncMock(return_value=_Formulier(FORMULIER))
    return verzoek


def _gebruikersdienst() -> Any:
    dienst = MagicMock()
    dienst.is_platform_admin.side_effect = lambda email: email == BEHEERDER
    dienst.add_allowed_email = MagicMock()
    dienst.remove_allowed_email = MagicMock()
    return dienst


def _beheerdienst() -> Any:
    dienst = MagicMock()
    dienst.get_user = AsyncMock(return_value={"id": USER_ID, "email": ANDER, "full_name": "Ander Iemand"})
    dienst.create_user = AsyncMock(return_value={"id": USER_ID})
    dienst.update_user = AsyncMock(return_value={"id": USER_ID})
    dienst.delete_user = AsyncMock(return_value=True)
    return dienst


def _otp_dienst() -> Any:
    return MagicMock(reset=AsyncMock(return_value=True))


async def _roep(route: str, verzoek: Any, beheer: Any, otp: Any) -> Any:
    """Roep een van de vijf routes aan met alle diensten eromheen gemockt."""
    handler = getattr(router_user_admin, route)
    with (
        patch("opi.services.user_service.get_user_service", return_value=_gebruikersdienst()),
        patch("opi.web.router_user_admin.get_user_service", return_value=_gebruikersdienst()),
        patch("opi.web.router_user_admin._get_service", return_value=beheer),
        patch("opi.web.router_user_admin._get_otp_service", return_value=otp),
        patch("opi.web.router_user_admin.revoke_sessions", AsyncMock(return_value=datetime.now(UTC))),
    ):
        if route == "create_user_submit":
            return await handler(verzoek)
        return await handler(verzoek, USER_ID)


#: De route, de handeling die hij oplegt, en de dienstaanroep die niet mag gebeuren.
ROUTES = [
    ("create_user_submit", StepUpAction.USER_CREATE, "create_user"),
    ("edit_user_submit", StepUpAction.USER_EDIT, "update_user"),
    ("revoke_user_sessions", StepUpAction.USER_SESSIONS_REVOKE, None),
    ("delete_user", StepUpAction.USER_DELETE, "delete_user"),
    ("reset_user_otp", StepUpAction.USER_OTP_RESET, None),
]


@pytest.mark.parametrize(("route", "actie", "dienstaanroep"), ROUTES)
async def test_zonder_verse_step_up_gebeurt_er_niets(
    route: str, actie: StepUpAction, dienstaanroep: str | None
) -> None:
    beheer, otp = _beheerdienst(), _otp_dienst()

    with pytest.raises(HTTPException) as fout:
        await _roep(route, _verzoek(verse_step_up=False), beheer, otp)

    assert fout.value.status_code == 403
    assert actie.phrase in str(fout.value.detail)
    if dienstaanroep:
        getattr(beheer, dienstaanroep).assert_not_awaited()
    otp.reset.assert_not_awaited()


@pytest.mark.parametrize(("route", "actie", "dienstaanroep"), ROUTES)
async def test_met_een_verse_step_up_loopt_de_route_door(
    route: str, actie: StepUpAction, dienstaanroep: str | None
) -> None:
    """De negatieve controle: zonder deze regel zou de toets hierboven ook groen zijn als
    de route om een heel andere reden weigert."""
    beheer, otp = _beheerdienst(), _otp_dienst()

    antwoord = await _roep(route, _verzoek(verse_step_up=True), beheer, otp)

    assert antwoord.status_code == 303, f"{route} kwam niet door: {antwoord.status_code}"
    if dienstaanroep:
        getattr(beheer, dienstaanroep).assert_awaited_once()


@pytest.mark.parametrize("route", ["create_user_submit", "edit_user_submit"])
async def test_de_dialoog_herhaalt_de_ingevulde_velden(route: str) -> None:
    """Zonder die velden zou de gebruiker zijn formulier na de code opnieuw moeten typen."""
    dialoog = AsyncMock(return_value="<div>dialoog</div>")

    with patch("opi.web.step_up_dialog.render_step_up_dialog", dialoog):
        antwoord = await _roep(route, _verzoek(verse_step_up=False, htmx=True), _beheerdienst(), _otp_dienst())

    assert antwoord.status_code == 200
    velden = dialoog.await_args.kwargs["vervolg_velden"]
    assert set(velden) == set(_FORMULIERVELDEN)
    assert velden["email"] == ANDER


#: De pagina, de formulieren erop die een step-up oproepen, en waar hun antwoord landt.
#: Het formulier mikt op zichzelf: een veldfout hoort te staan waar het formulier staat,
#: en de weigering van de step-up stuurt met HX-Retarget zelf naar de dialoog.
PAGINAS = [
    ("bg/admin-users.html.j2", ["revoke-sessions", "delete", "otp-reset"], "#edit-section-inner"),
    ("bg/_admin-user-form.html.j2", ["{{ form_action }}"], "this"),
]


#: Het commentaar gaat eraf voor de assertie: het noemt hx-target="this" zelf, en dan
#: zou de toets groen blijven met het attribuut weg.
JINJA_COMMENTAAR = re.compile(r"\{#.*?#\}", re.DOTALL)


@pytest.mark.parametrize(("sjabloon", "bestemmingen", "doel"), PAGINAS)
def test_elk_formulier_post_met_htmx(sjabloon: str, bestemmingen: list[str], doel: str) -> None:
    """Anders komt de dialoog niet: de weigering rendert hem alleen op een HX-Request."""
    tekst = JINJA_COMMENTAAR.sub("", (SJABLONEN / sjabloon).read_text())

    for bestemming in bestemmingen:
        assert (
            f'hx-post="{bestemming}"' in tekst or f'hx-post="/admin/users/{{{{ user.id }}}}/{bestemming}"' in tekst
        ), f"{sjabloon} post {bestemming} zonder htmx"
    assert f'hx-target="{doel}"' in tekst, f"{sjabloon} mist hx-target={doel}"


@pytest.mark.parametrize("sjabloon", ["bg/admin-users.html.j2", "bg/admin-user-form.html.j2"])
def test_elke_pagina_draagt_de_schil_voor_de_dialoog(sjabloon: str) -> None:
    """Zonder schil belandt de dialoog nergens en staat de beheerder met leeg scherm."""
    tekst = (SJABLONEN / sjabloon).read_text()

    assert "bg/_step-up-venster.html.j2" in tekst, f"{sjabloon} mist de schil voor de step-up-dialoog"


def test_de_schil_heeft_alles_wat_edit_modal_js_zoekt() -> None:
    """De dialoog opent op htmx:afterSwap, en dat script selecteert op deze namen."""
    tekst = (SJABLONEN / "bg/_step-up-venster.html.j2").read_text()

    for naam in (
        'id="edit-section-backdrop"',
        'id="edit-section-modal"',
        'id="edit-section-inner"',
        'id="edit-section-title-text"',
        'class="edit-section-modal"',
        "js/edit_modal.js",
    ):
        assert naam in tekst, f"de schil mist {naam}"


@pytest.mark.parametrize(("route", "actie", "_dienstaanroep"), ROUTES)
async def test_zonder_htmx_komt_de_beheerder_bij_een_invulbaar_codeveld(
    route: str, actie: StepUpAction, _dienstaanroep: str | None
) -> None:
    """De productievorm: een gewone POST krijgt een 403, dus de dialoog MOET van htmx komen.

    Deze toets legt die grens vast. Hij meet de 403 aan de ene kant en aan de andere kant
    dat de weigering op een HX-Request een echt OTP-veld rendert, zodat de twee samen
    bewijzen waarom de formulieren hierboven met htmx posten.
    """
    with pytest.raises(HTTPException) as fout:
        await _roep(route, _verzoek(verse_step_up=False), _beheerdienst(), _otp_dienst())
    assert fout.value.status_code == 403

    status = SimpleNamespace(registered=True)
    with patch("opi.services.user_otp_service.UserOtpService.status", AsyncMock(return_value=status)):
        antwoord = await _roep(route, _verzoek(verse_step_up=False, htmx=True), _beheerdienst(), _otp_dienst())

    html = antwoord.body.decode()
    assert antwoord.headers["HX-Retarget"] == "#edit-section-inner"
    assert 'name="otp"' in html, "geen codeveld in de dialoog"
    assert actie.phrase in html


def test_de_twee_leesroutes_vragen_geen_step_up() -> None:
    """Een formulier tonen is geen wijziging; de API doet dat voor zijn leesroutes ook niet."""
    from opi.core.auth_decorators import REQUIRES_STEP_UP

    for route in ("list_users", "create_user_form", "edit_user_form"):
        handler = getattr(router_user_admin, route)
        assert not hasattr(handler, REQUIRES_STEP_UP), f"{route} is een leesroute"


@pytest.mark.parametrize(("route", "actie", "_dienstaanroep"), ROUTES)
def test_elke_wijzigende_route_is_gemarkeerd(route: str, actie: StepUpAction, _dienstaanroep: str | None) -> None:
    """De markering die ``custom_openapi`` en ``test_step_up_routes.py`` lezen."""
    from opi.core.auth_decorators import REQUIRES_STEP_UP

    assert getattr(getattr(router_user_admin, route), REQUIRES_STEP_UP) is actie
