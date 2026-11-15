"""De webroutes van het tabblad Team: leden, rollen en tokens.

Legt vast waar ``tests/e2e/test_tokens_tabblad.py`` niet bij komt: die browsertest meet
dat de formulieren renderen. Hier staat de weg van de POST naar het formulier en terug:
dat de sessie na een geslaagde uitgifte alleen een VERWIJZING draagt en niet de waarde (de
cookie-sessie is ondertekend en niet versleuteld), dat de waarde precies een keer op te
halen is, en dat een weigering in de sessie terechtkomt in plaats van een 500.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bs4 import BeautifulSoup
from fastapi import HTTPException
from opi.http.auth.step_up_dialog import MODAL_TARGET
from opi.http.render.templates_lotc import templates_lotc
from opi.services.api_token_service import TokenRequestRefusedError
from opi.services.step_up import STEP_UP_SESSION_KEY
from opi.web.router_tokens import (
    NEW_TOKEN_SESSION_KEY,
    TOKEN_ERROR_SESSION_KEY,
    _email,
    _hold_for_display,
    _pending,
    _take_once,
    maak_token_web,
    roteer_token_web,
    tokencontext,
    trek_token_in_web,
    verwijder_service_account_web,
    voeg_service_account_toe_web,
    wijzig_rol_web,
)

pytestmark = pytest.mark.asyncio

PROJECT = "bouwput"
EMAIL = "beheerder@rijksoverheid.nl"


def _verzoek(email: str | None, velden: dict[str, str], *, step_up: bool = True, htmx: bool = False) -> Any:
    """Een verzoek zoals de route hem ziet, met een verse step-up tenzij anders gevraagd.

    Elke handeling hier vraagt er een (RC-387), dus zonder stempel meet een test de
    weigering en niet de handeling zelf.
    """
    user = {"email": email} if email else None
    sessie = {STEP_UP_SESSION_KEY: datetime.now(UTC).isoformat()} if step_up else {}
    return SimpleNamespace(
        state=SimpleNamespace(user=user),
        session=sessie,
        method="POST",
        url=SimpleNamespace(path="/projects/bouwput/leden"),
        headers={"HX-Request": "true"} if htmx else {},
        form=AsyncMock(return_value=velden),
    )


# --- uitgeven --------------------------------------------------------------


async def test_een_uitgegeven_token_is_precies_een_keer_op_te_halen() -> None:
    """De sessiecookie is ondertekend en niet versleuteld, dus de waarde hoort er niet in."""
    with patch("opi.web.router_tokens.issue_token", AsyncMock(return_value="zad_geheim")) as mock:
        verzoek = _verzoek(EMAIL, {"tokennaam": "cli", "dagen": "30"})
        antwoord = await maak_token_web(verzoek, PROJECT, EMAIL)

    assert antwoord.status_code == 303
    verwijzing = verzoek.session[NEW_TOKEN_SESSION_KEY]
    assert "zad_geheim" not in verwijzing
    assert _take_once(verwijzing) == {"token": "zad_geheim", "eigenaar": EMAIL}
    assert _take_once(verwijzing) is None, "een tweede ophaling hoort niets meer te geven"
    assert TOKEN_ERROR_SESSION_KEY not in verzoek.session
    mock.assert_awaited_once_with(project_name=PROJECT, name=EMAIL, token_name="cli", days=30, by=EMAIL)


async def test_roteren_geeft_de_nieuwe_waarde_terug() -> None:
    with patch("opi.web.router_tokens.rotate_token", AsyncMock(return_value="zad_nieuw")) as mock:
        verzoek = _verzoek(EMAIL, {"dagen": "30"})
        antwoord = await roteer_token_web(verzoek, PROJECT, "bouwstraat", "pijplijn")

    assert antwoord.status_code == 303
    assert _take_once(verzoek.session[NEW_TOKEN_SESSION_KEY]) == {"token": "zad_nieuw", "eigenaar": "bouwstraat"}
    mock.assert_awaited_once_with(project_name=PROJECT, name="bouwstraat", token_name="pijplijn", days=30, by=EMAIL)


async def test_een_niet_numeriek_aantal_dagen_komt_in_de_sessie_en_geeft_geen_500() -> None:
    """``int()`` op een geknoeid formulierveld is een ``ValueError``, geen 500."""
    verzoek = _verzoek(EMAIL, {"tokennaam": "cli", "dagen": "niet-een-getal"})
    antwoord = await maak_token_web(verzoek, PROJECT, EMAIL)

    assert antwoord.status_code == 303
    assert verzoek.session[TOKEN_ERROR_SESSION_KEY] == "Vul een aantal dagen in."
    assert NEW_TOKEN_SESSION_KEY not in verzoek.session


async def test_een_weigering_komt_in_de_sessie_en_geeft_geen_500() -> None:
    """De rolregel weigert in de service; de webroute maakt er een boodschap van."""
    with patch(
        "opi.web.router_tokens.issue_token",
        AsyncMock(side_effect=TokenRequestRefusedError("Je kunt alleen een token voor jezelf maken.")),
    ):
        verzoek = _verzoek("dev@rijksoverheid.nl", {"tokennaam": "cli", "dagen": "30"})
        antwoord = await maak_token_web(verzoek, PROJECT, EMAIL)

    assert antwoord.status_code == 303
    assert verzoek.session[TOKEN_ERROR_SESSION_KEY] == "Je kunt alleen een token voor jezelf maken."
    assert NEW_TOKEN_SESSION_KEY not in verzoek.session


async def test_roteren_weigering_komt_in_de_sessie_en_geeft_geen_500() -> None:
    with patch(
        "opi.web.router_tokens.rotate_token",
        AsyncMock(side_effect=TokenRequestRefusedError("alleen een token voor jezelf maken")),
    ):
        verzoek = _verzoek(EMAIL, {"dagen": "30"})
        antwoord = await roteer_token_web(verzoek, PROJECT, "bouwstraat", "pijplijn")

    assert antwoord.status_code == 303
    assert verzoek.session[TOKEN_ERROR_SESSION_KEY] == "alleen een token voor jezelf maken"
    assert NEW_TOKEN_SESSION_KEY not in verzoek.session


def test_de_cleanup_ruimt_een_verlopen_pendinge_waarde_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zonder deze opruiming blijft een nooit-opgehaalde waarde in het geheugen staan."""
    _pending.clear()
    monkeypatch.setattr("opi.web.router_tokens.time.monotonic", lambda: 1000.0)
    _hold_for_display({"token": "zad_verlopen", "eigenaar": "x"})

    monkeypatch.setattr("opi.web.router_tokens.time.monotonic", lambda: 2000.0)
    _hold_for_display({"token": "zad_nieuw", "eigenaar": "y"})

    assert [details["token"] for _, details in _pending.values()] == ["zad_nieuw"]


# --- _email ------------------------------------------------------------------


def test_email_zonder_aangemelde_gebruiker_geeft_403() -> None:
    verzoek = _verzoek(None, {})

    with pytest.raises(HTTPException) as info:
        _email(verzoek)

    assert info.value.status_code == 403


# --- intrekken en leden ----------------------------------------------------


async def test_intrekken_gaat_langs_de_service() -> None:
    with patch("opi.web.router_tokens.revoke_token", AsyncMock()) as mock:
        verzoek = _verzoek(EMAIL, {})
        antwoord = await trek_token_in_web(verzoek, PROJECT, "bouwstraat", "pijplijn")

    assert antwoord.status_code == 303
    mock.assert_awaited_once_with(project_name=PROJECT, name="bouwstraat", token_name="pijplijn", by=EMAIL)
    assert TOKEN_ERROR_SESSION_KEY not in verzoek.session


async def test_intrekken_weigering_komt_in_de_sessie_en_geeft_geen_500() -> None:
    with patch(
        "opi.web.router_tokens.revoke_token",
        AsyncMock(side_effect=TokenRequestRefusedError("Alleen een beheerder van dit project kan dit doen.")),
    ):
        verzoek = _verzoek("dev@rijksoverheid.nl", {})
        antwoord = await trek_token_in_web(verzoek, PROJECT, "bouwstraat", "pijplijn")

    assert antwoord.status_code == 303
    assert verzoek.session[TOKEN_ERROR_SESSION_KEY] == "Alleen een beheerder van dit project kan dit doen."


async def test_een_service_account_toevoegen() -> None:
    with patch("opi.web.router_tokens.add_service_account", AsyncMock()) as mock:
        verzoek = _verzoek(EMAIL, {"naam": "bouwstraat", "roles": "developer"})
        antwoord = await voeg_service_account_toe_web(verzoek, PROJECT)

    assert antwoord.status_code == 303
    mock.assert_awaited_once_with(project_name=PROJECT, name="bouwstraat", roles=["developer"], by=EMAIL)


async def test_een_service_account_toevoegen_weigering_komt_in_de_sessie_en_geeft_geen_500() -> None:
    with patch(
        "opi.web.router_tokens.add_service_account",
        AsyncMock(side_effect=TokenRequestRefusedError("Alleen een beheerder van dit project kan dit doen.")),
    ):
        verzoek = _verzoek("dev@rijksoverheid.nl", {"naam": "bouwstraat", "roles": "developer"})
        antwoord = await voeg_service_account_toe_web(verzoek, PROJECT)

    assert antwoord.status_code == 303
    assert verzoek.session[TOKEN_ERROR_SESSION_KEY] == "Alleen een beheerder van dit project kan dit doen."


async def test_een_service_account_verwijderen() -> None:
    with patch("opi.web.router_tokens.remove_service_account", AsyncMock()) as mock:
        verzoek = _verzoek(EMAIL, {})
        antwoord = await verwijder_service_account_web(verzoek, PROJECT, "bouwstraat")

    assert antwoord.status_code == 303
    mock.assert_awaited_once_with(project_name=PROJECT, name="bouwstraat", by=EMAIL)
    assert TOKEN_ERROR_SESSION_KEY not in verzoek.session


async def test_een_geweigerde_verwijdering_komt_in_de_sessie_en_geeft_geen_500() -> None:
    with patch(
        "opi.web.router_tokens.remove_service_account",
        AsyncMock(side_effect=TokenRequestRefusedError("Alleen een beheerder van dit project kan dit doen.")),
    ):
        verzoek = _verzoek("dev@rijksoverheid.nl", {})
        antwoord = await verwijder_service_account_web(verzoek, PROJECT, "bouwstraat")

    assert antwoord.status_code == 303
    assert verzoek.session[TOKEN_ERROR_SESSION_KEY] == "Alleen een beheerder van dit project kan dit doen."


async def test_een_rol_wijzigen() -> None:
    with patch("opi.web.router_tokens.set_member_roles", AsyncMock()) as mock:
        verzoek = _verzoek(EMAIL, {"roles": "admin,ops"})
        antwoord = await wijzig_rol_web(verzoek, PROJECT, "bouwstraat")

    assert antwoord.status_code == 303
    mock.assert_awaited_once_with(project_name=PROJECT, name="bouwstraat", roles=["admin", "ops"], by=EMAIL)


async def test_een_rol_wijzigen_weigering_komt_in_de_sessie_en_geeft_geen_500() -> None:
    with patch(
        "opi.web.router_tokens.set_member_roles",
        AsyncMock(side_effect=TokenRequestRefusedError("Onbekende rol 'root'.")),
    ):
        verzoek = _verzoek("dev@rijksoverheid.nl", {"roles": "root"})
        antwoord = await wijzig_rol_web(verzoek, PROJECT, "bouwstraat")

    assert antwoord.status_code == 303
    assert verzoek.session[TOKEN_ERROR_SESSION_KEY] == "Onbekende rol 'root'."


# --- wat het tabblad te zien krijgt ----------------------------------------


def _overzicht() -> list[dict[str, Any]]:
    vervalt = datetime.now(UTC) + timedelta(days=30)
    return [
        {
            "naam": EMAIL,
            "soort": "person",
            "rollen": ["admin", "ops"],
            "tokens": [{"naam": "cli", "vervalt": vervalt, "verlopen": False}],
        },
        {"naam": "dev@rijksoverheid.nl", "soort": "person", "rollen": ["developer"], "tokens": []},
        {"naam": "bouwstraat", "soort": "service-account", "rollen": ["developer"], "tokens": []},
    ]


def _overzicht_met_tokens() -> list[dict[str, Any]]:
    """Adds a token to bouwstraat too: rotate forms only render for a member that has one."""
    overzicht = _overzicht()
    vervalt = datetime.now(UTC) + timedelta(days=60)
    for lid in overzicht:
        if lid["naam"] == "bouwstraat":
            lid["tokens"] = [{"naam": "pijplijn", "vervalt": vervalt, "verlopen": False}]
    return overzicht


async def test_een_beheerder_beheert_elk_lid() -> None:
    verzoek = _verzoek(EMAIL, {})
    with patch("opi.web.router_tokens.members_overview", return_value=_overzicht()):
        context = tokencontext(verzoek, PROJECT, EMAIL, is_admin=True)

    per_naam = {lid["naam"]: lid for lid in context["leden"]}
    assert per_naam["bouwstraat"]["mag_uitgeven"] is True
    assert per_naam["dev@rijksoverheid.nl"]["mag_intrekken"] is True
    assert context["is_project_beheerder"] is True


async def test_een_mens_beheert_alleen_zichzelf() -> None:
    """Een token voor een ander mens maken kan niemand: dat zou een bewijsmiddel zijn dat
    iemand draagt zonder te weten dat het bestaat."""
    dev = "dev@rijksoverheid.nl"
    verzoek = _verzoek(dev, {})
    with patch("opi.web.router_tokens.members_overview", return_value=_overzicht()):
        context = tokencontext(verzoek, PROJECT, dev, is_admin=False)

    per_naam = {lid["naam"]: lid for lid in context["leden"]}
    assert per_naam[dev]["mag_uitgeven"] is True
    assert per_naam[EMAIL]["mag_uitgeven"] is False
    assert per_naam[EMAIL]["mag_intrekken"] is False
    assert per_naam["bouwstraat"]["mag_uitgeven"] is False


async def test_het_tabblad_toont_de_platformmaxima() -> None:
    """Het maximum is platformbeleid en geen keuze van wie het token maakt."""
    verzoek = _verzoek(EMAIL, {})
    with patch("opi.web.router_tokens.members_overview", return_value=[]):
        context = tokencontext(verzoek, PROJECT, EMAIL, is_admin=True)

    assert context["token_max_dagen_persoonlijk"] > 0
    assert context["token_max_dagen_service_account"] >= context["token_max_dagen_persoonlijk"]


# --- the caller's own card -------------------------------------------------


def _render_tokens_tab(context: dict[str, Any]) -> BeautifulSoup:
    html = templates_lotc.env.get_template("bg/_tokens.html.j2").render(
        project=SimpleNamespace(name=PROJECT),
        csrf_token="csrf-van-de-test",
        **context,
    )
    return BeautifulSoup(html, "html.parser")


async def test_the_current_users_card_comes_first_and_keeps_the_project_file_order_for_the_rest() -> None:
    dev = "dev@rijksoverheid.nl"
    verzoek = _verzoek(dev, {})
    with patch("opi.web.router_tokens.members_overview", return_value=_overzicht()):
        context = tokencontext(verzoek, PROJECT, dev, is_admin=False)

    assert [lid["naam"] for lid in context["leden"]] == [dev, EMAIL, "bouwstraat"]
    assert [lid["is_current_user"] for lid in context["leden"]] == [True, False, False]


async def test_a_caller_who_is_no_member_has_no_own_card_and_moves_nothing() -> None:
    """A platform admin viewing the project without being a member has no own card to find."""
    buitenstaander = "buitenstaander@rijksoverheid.nl"
    verzoek = _verzoek(buitenstaander, {})
    with patch("opi.web.router_tokens.members_overview", return_value=_overzicht()):
        context = tokencontext(verzoek, PROJECT, buitenstaander, is_admin=False)

    assert [lid["naam"] for lid in context["leden"]] == [EMAIL, "dev@rijksoverheid.nl", "bouwstraat"]
    assert all(lid["is_current_user"] is False for lid in context["leden"])


async def test_the_own_card_is_first_on_the_page_and_carries_a_marker() -> None:
    dev = "dev@rijksoverheid.nl"
    verzoek = _verzoek(dev, {})
    with patch("opi.web.router_tokens.members_overview", return_value=_overzicht()):
        soup = _render_tokens_tab(tokencontext(verzoek, PROJECT, dev, is_admin=False))

    koppen = soup.select("nldd-title h3")
    assert [kop.get_text(strip=True) for kop in koppen[:3]] == [dev, EMAIL, "bouwstraat"]

    eigen_kopregel = koppen[0].find_parent(attrs={"data-lotc-component": "cluster"})
    assert eigen_kopregel is not None, "the caller's heading has no marker cluster around it"
    markering = eigen_kopregel.find(attrs={"data-lotc-component": "tag"})
    assert markering is not None
    assert markering.get("text") == "jij"
    assert len(soup.select('[data-lotc-component="tag"][text="jij"]')) == 1


async def test_the_own_card_shows_its_forms_directly_and_other_cards_keep_the_accordion() -> None:
    verzoek = _verzoek(EMAIL, {})
    with patch("opi.web.router_tokens.members_overview", return_value=_overzicht_met_tokens()):
        soup = _render_tokens_tab(tokencontext(verzoek, PROJECT, EMAIL, is_admin=True))

    eigen_uitgeven = soup.find("form", action=f"/projects/{PROJECT}/leden/{EMAIL}/tokens")
    eigen_roteren = soup.find("form", action=f"/projects/{PROJECT}/leden/{EMAIL}/tokens/cli/roteren")
    for formulier in (eigen_uitgeven, eigen_roteren):
        assert formulier is not None
        assert formulier.find_parent(attrs={"data-lotc-component": "accordion-item"}) is None

    ander_uitgeven = soup.find("form", action=f"/projects/{PROJECT}/leden/bouwstraat/tokens")
    ander_roteren = soup.find("form", action=f"/projects/{PROJECT}/leden/bouwstraat/tokens/pijplijn/roteren")
    for formulier in (ander_uitgeven, ander_roteren):
        assert formulier is not None
        assert formulier.find_parent(attrs={"data-lotc-component": "accordion-item"}) is not None


# --- de step-up ------------------------------------------------------------


async def test_zonder_verse_step_up_gebeurt_er_niets() -> None:
    """Elke handeling hier vraagt een verse factor (RC-387), ook buiten productie."""
    verzoek = _verzoek(EMAIL, {"tokennaam": "cli", "dagen": "30"}, step_up=False)
    with patch("opi.web.router_tokens.issue_token", AsyncMock()) as mock, pytest.raises(HTTPException) as info:
        await maak_token_web(verzoek, PROJECT, EMAIL)

    assert info.value.status_code == 403
    mock.assert_not_awaited()


async def test_de_dialoog_draagt_de_velden_van_het_formulier_mee() -> None:
    """De dialoog neemt de plaats van het formulier in, dus de herhaling kent zijn velden."""
    dialoog = AsyncMock(return_value="<dialoog>")
    with (
        patch("opi.web.router_tokens.issue_token", AsyncMock()) as mock,
        patch("opi.http.auth.step_up_dialog.render_step_up_dialog", dialoog),
    ):
        verzoek = _verzoek(EMAIL, {"tokennaam": "cli", "dagen": "30"}, step_up=False, htmx=True)
        antwoord = await maak_token_web(verzoek, PROJECT, EMAIL)

    assert antwoord.headers["HX-Retarget"] == MODAL_TARGET
    mock.assert_not_awaited()
    assert dialoog.await_args is not None
    assert dialoog.await_args.kwargs["follow_fields"] == {"tokennaam": "cli", "dagen": "30"}
