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
from fastapi import HTTPException
from opi.core.step_up import STEP_UP_SESSION_KEY
from opi.services.api_token_service import TokenAanvraagGeweigerd
from opi.web.router_tokens import (
    NIEUW_TOKEN_SLEUTEL,
    TOKENFOUT_SLEUTEL,
    _email,
    _haal_op,
    _leg_klaar,
    _wachtend,
    maak_token_web,
    roteer_token_web,
    tokencontext,
    trek_token_in_web,
    verwijder_service_account_web,
    voeg_service_account_toe_web,
    wijzig_rol_web,
)
from opi.web.step_up_dialog import MODAL_TARGET

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
        url=SimpleNamespace(path="/projects/bouwput/leden"),
        headers={"HX-Request": "true"} if htmx else {},
        form=AsyncMock(return_value=velden),
    )


# --- uitgeven --------------------------------------------------------------


async def test_een_uitgegeven_token_is_precies_een_keer_op_te_halen() -> None:
    """De sessiecookie is ondertekend en niet versleuteld, dus de waarde hoort er niet in."""
    with patch("opi.web.router_tokens.geef_token_uit", AsyncMock(return_value="zad_geheim")) as mock:
        verzoek = _verzoek(EMAIL, {"tokennaam": "cli", "dagen": "30"})
        antwoord = await maak_token_web(verzoek, PROJECT, EMAIL)

    assert antwoord.status_code == 303
    verwijzing = verzoek.session[NIEUW_TOKEN_SLEUTEL]
    assert "zad_geheim" not in verwijzing
    assert _haal_op(verwijzing) == {"token": "zad_geheim", "eigenaar": EMAIL}
    assert _haal_op(verwijzing) is None, "een tweede ophaling hoort niets meer te geven"
    assert TOKENFOUT_SLEUTEL not in verzoek.session
    mock.assert_awaited_once_with(project_name=PROJECT, naam=EMAIL, tokennaam="cli", dagen=30, door=EMAIL)


async def test_roteren_geeft_de_nieuwe_waarde_terug() -> None:
    with patch("opi.web.router_tokens.roteer_token", AsyncMock(return_value="zad_nieuw")) as mock:
        verzoek = _verzoek(EMAIL, {"dagen": "30"})
        antwoord = await roteer_token_web(verzoek, PROJECT, "bouwstraat", "pijplijn")

    assert antwoord.status_code == 303
    assert _haal_op(verzoek.session[NIEUW_TOKEN_SLEUTEL]) == {"token": "zad_nieuw", "eigenaar": "bouwstraat"}
    mock.assert_awaited_once_with(project_name=PROJECT, naam="bouwstraat", tokennaam="pijplijn", dagen=30, door=EMAIL)


async def test_een_niet_numeriek_aantal_dagen_komt_in_de_sessie_en_geeft_geen_500() -> None:
    """``int()`` op een geknoeid formulierveld is een ``ValueError``, geen 500."""
    verzoek = _verzoek(EMAIL, {"tokennaam": "cli", "dagen": "niet-een-getal"})
    antwoord = await maak_token_web(verzoek, PROJECT, EMAIL)

    assert antwoord.status_code == 303
    assert verzoek.session[TOKENFOUT_SLEUTEL] == "Vul een aantal dagen in."
    assert NIEUW_TOKEN_SLEUTEL not in verzoek.session


async def test_een_weigering_komt_in_de_sessie_en_geeft_geen_500() -> None:
    """De rolregel weigert in de service; de webroute maakt er een boodschap van."""
    with patch(
        "opi.web.router_tokens.geef_token_uit",
        AsyncMock(side_effect=TokenAanvraagGeweigerd("Je kunt alleen een token voor jezelf maken.")),
    ):
        verzoek = _verzoek("dev@rijksoverheid.nl", {"tokennaam": "cli", "dagen": "30"})
        antwoord = await maak_token_web(verzoek, PROJECT, EMAIL)

    assert antwoord.status_code == 303
    assert verzoek.session[TOKENFOUT_SLEUTEL] == "Je kunt alleen een token voor jezelf maken."
    assert NIEUW_TOKEN_SLEUTEL not in verzoek.session


async def test_roteren_weigering_komt_in_de_sessie_en_geeft_geen_500() -> None:
    with patch(
        "opi.web.router_tokens.roteer_token",
        AsyncMock(side_effect=TokenAanvraagGeweigerd("alleen een token voor jezelf maken")),
    ):
        verzoek = _verzoek(EMAIL, {"dagen": "30"})
        antwoord = await roteer_token_web(verzoek, PROJECT, "bouwstraat", "pijplijn")

    assert antwoord.status_code == 303
    assert verzoek.session[TOKENFOUT_SLEUTEL] == "alleen een token voor jezelf maken"
    assert NIEUW_TOKEN_SLEUTEL not in verzoek.session


def test_de_cleanup_ruimt_een_verlopen_wachtende_waarde_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zonder deze opruiming blijft een nooit-opgehaalde waarde in het geheugen staan."""
    _wachtend.clear()
    monkeypatch.setattr("opi.web.router_tokens.time.monotonic", lambda: 1000.0)
    _leg_klaar({"token": "zad_verlopen", "eigenaar": "x"})

    monkeypatch.setattr("opi.web.router_tokens.time.monotonic", lambda: 2000.0)
    _leg_klaar({"token": "zad_nieuw", "eigenaar": "y"})

    assert [details["token"] for _, details in _wachtend.values()] == ["zad_nieuw"]


# --- _email ------------------------------------------------------------------


def test_email_zonder_aangemelde_gebruiker_geeft_403() -> None:
    verzoek = _verzoek(None, {})

    with pytest.raises(HTTPException) as info:
        _email(verzoek)

    assert info.value.status_code == 403


# --- intrekken en leden ----------------------------------------------------


async def test_intrekken_gaat_langs_de_service() -> None:
    with patch("opi.web.router_tokens.trek_token_in", AsyncMock()) as mock:
        verzoek = _verzoek(EMAIL, {})
        antwoord = await trek_token_in_web(verzoek, PROJECT, "bouwstraat", "pijplijn")

    assert antwoord.status_code == 303
    mock.assert_awaited_once_with(project_name=PROJECT, naam="bouwstraat", tokennaam="pijplijn", door=EMAIL)
    assert TOKENFOUT_SLEUTEL not in verzoek.session


async def test_intrekken_weigering_komt_in_de_sessie_en_geeft_geen_500() -> None:
    with patch(
        "opi.web.router_tokens.trek_token_in",
        AsyncMock(side_effect=TokenAanvraagGeweigerd("Alleen een beheerder van dit project kan dit doen.")),
    ):
        verzoek = _verzoek("dev@rijksoverheid.nl", {})
        antwoord = await trek_token_in_web(verzoek, PROJECT, "bouwstraat", "pijplijn")

    assert antwoord.status_code == 303
    assert verzoek.session[TOKENFOUT_SLEUTEL] == "Alleen een beheerder van dit project kan dit doen."


async def test_een_service_account_toevoegen() -> None:
    with patch("opi.web.router_tokens.voeg_service_account_toe", AsyncMock()) as mock:
        verzoek = _verzoek(EMAIL, {"naam": "bouwstraat", "rol": "developer"})
        antwoord = await voeg_service_account_toe_web(verzoek, PROJECT)

    assert antwoord.status_code == 303
    mock.assert_awaited_once_with(project_name=PROJECT, naam="bouwstraat", rol="developer", door=EMAIL)


async def test_een_service_account_toevoegen_weigering_komt_in_de_sessie_en_geeft_geen_500() -> None:
    with patch(
        "opi.web.router_tokens.voeg_service_account_toe",
        AsyncMock(side_effect=TokenAanvraagGeweigerd("Alleen een beheerder van dit project kan dit doen.")),
    ):
        verzoek = _verzoek("dev@rijksoverheid.nl", {"naam": "bouwstraat", "rol": "developer"})
        antwoord = await voeg_service_account_toe_web(verzoek, PROJECT)

    assert antwoord.status_code == 303
    assert verzoek.session[TOKENFOUT_SLEUTEL] == "Alleen een beheerder van dit project kan dit doen."


async def test_een_service_account_verwijderen() -> None:
    with patch("opi.web.router_tokens.verwijder_service_account", AsyncMock()) as mock:
        verzoek = _verzoek(EMAIL, {})
        antwoord = await verwijder_service_account_web(verzoek, PROJECT, "bouwstraat")

    assert antwoord.status_code == 303
    mock.assert_awaited_once_with(project_name=PROJECT, naam="bouwstraat", door=EMAIL)
    assert TOKENFOUT_SLEUTEL not in verzoek.session


async def test_een_geweigerde_verwijdering_komt_in_de_sessie_en_geeft_geen_500() -> None:
    with patch(
        "opi.web.router_tokens.verwijder_service_account",
        AsyncMock(side_effect=TokenAanvraagGeweigerd("Alleen een beheerder van dit project kan dit doen.")),
    ):
        verzoek = _verzoek("dev@rijksoverheid.nl", {})
        antwoord = await verwijder_service_account_web(verzoek, PROJECT, "bouwstraat")

    assert antwoord.status_code == 303
    assert verzoek.session[TOKENFOUT_SLEUTEL] == "Alleen een beheerder van dit project kan dit doen."


async def test_een_rol_wijzigen() -> None:
    with patch("opi.web.router_tokens.wijzig_rol", AsyncMock()) as mock:
        verzoek = _verzoek(EMAIL, {"rol": "admin"})
        antwoord = await wijzig_rol_web(verzoek, PROJECT, "bouwstraat")

    assert antwoord.status_code == 303
    mock.assert_awaited_once_with(project_name=PROJECT, naam="bouwstraat", rol="admin", door=EMAIL)


async def test_een_rol_wijzigen_weigering_komt_in_de_sessie_en_geeft_geen_500() -> None:
    with patch(
        "opi.web.router_tokens.wijzig_rol", AsyncMock(side_effect=TokenAanvraagGeweigerd("Onbekende rol 'root'."))
    ):
        verzoek = _verzoek("dev@rijksoverheid.nl", {"rol": "root"})
        antwoord = await wijzig_rol_web(verzoek, PROJECT, "bouwstraat")

    assert antwoord.status_code == 303
    assert verzoek.session[TOKENFOUT_SLEUTEL] == "Onbekende rol 'root'."


# --- wat het tabblad te zien krijgt ----------------------------------------


def _overzicht() -> list[dict[str, Any]]:
    vervalt = datetime.now(UTC) + timedelta(days=30)
    return [
        {
            "naam": EMAIL,
            "soort": "person",
            "rol": "admin",
            "tokens": [{"naam": "cli", "vervalt": vervalt, "verlopen": False}],
        },
        {"naam": "dev@rijksoverheid.nl", "soort": "person", "rol": "developer", "tokens": []},
        {"naam": "bouwstraat", "soort": "service-account", "rol": "developer", "tokens": []},
    ]


async def test_een_beheerder_beheert_elk_lid() -> None:
    verzoek = _verzoek(EMAIL, {})
    with patch("opi.web.router_tokens.leden_overzicht", return_value=_overzicht()):
        context = tokencontext(verzoek, PROJECT, EMAIL, is_beheerder=True)

    per_naam = {lid["naam"]: lid for lid in context["leden"]}
    assert per_naam["bouwstraat"]["mag_uitgeven"] is True
    assert per_naam["dev@rijksoverheid.nl"]["mag_intrekken"] is True
    assert context["is_project_beheerder"] is True


async def test_een_mens_beheert_alleen_zichzelf() -> None:
    """Een token voor een ander mens maken kan niemand: dat zou een bewijsmiddel zijn dat
    iemand draagt zonder te weten dat het bestaat."""
    dev = "dev@rijksoverheid.nl"
    verzoek = _verzoek(dev, {})
    with patch("opi.web.router_tokens.leden_overzicht", return_value=_overzicht()):
        context = tokencontext(verzoek, PROJECT, dev, is_beheerder=False)

    per_naam = {lid["naam"]: lid for lid in context["leden"]}
    assert per_naam[dev]["mag_uitgeven"] is True
    assert per_naam[EMAIL]["mag_uitgeven"] is False
    assert per_naam[EMAIL]["mag_intrekken"] is False
    assert per_naam["bouwstraat"]["mag_uitgeven"] is False


async def test_het_tabblad_toont_de_platformmaxima() -> None:
    """Het maximum is platformbeleid en geen keuze van wie het token maakt."""
    verzoek = _verzoek(EMAIL, {})
    with patch("opi.web.router_tokens.leden_overzicht", return_value=[]):
        context = tokencontext(verzoek, PROJECT, EMAIL, is_beheerder=True)

    assert context["token_max_dagen_persoonlijk"] > 0
    assert context["token_max_dagen_service_account"] >= context["token_max_dagen_persoonlijk"]


# --- de step-up ------------------------------------------------------------


async def test_zonder_verse_step_up_gebeurt_er_niets() -> None:
    """Elke handeling hier vraagt een verse factor (RC-387), ook buiten productie."""
    verzoek = _verzoek(EMAIL, {"tokennaam": "cli", "dagen": "30"}, step_up=False)
    with patch("opi.web.router_tokens.geef_token_uit", AsyncMock()) as mock, pytest.raises(HTTPException) as info:
        await maak_token_web(verzoek, PROJECT, EMAIL)

    assert info.value.status_code == 403
    mock.assert_not_awaited()


async def test_de_dialoog_draagt_de_velden_van_het_formulier_mee() -> None:
    """De dialoog neemt de plaats van het formulier in, dus de herhaling kent zijn velden."""
    dialoog = AsyncMock(return_value="<dialoog>")
    with (
        patch("opi.web.router_tokens.geef_token_uit", AsyncMock()) as mock,
        patch("opi.web.step_up_dialog.render_step_up_dialog", dialoog),
    ):
        verzoek = _verzoek(EMAIL, {"tokennaam": "cli", "dagen": "30"}, step_up=False, htmx=True)
        antwoord = await maak_token_web(verzoek, PROJECT, EMAIL)

    assert antwoord.headers["HX-Retarget"] == MODAL_TARGET
    mock.assert_not_awaited()
    assert dialoog.await_args is not None
    assert dialoog.await_args.kwargs["vervolg_velden"] == {"tokennaam": "cli", "dagen": "30"}
