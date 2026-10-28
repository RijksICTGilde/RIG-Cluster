"""De waarden die in de kaart van een component staan: omgevingsvariabelen en aliassen.

Beide stonden er niet. De omgevingsvariabelen hadden een regel die naar een sectie op een
ANDER tabblad verwees, de aliassen waren nergens terug te lezen: je kon ze invullen via
Bewerken op deze kaart, maar wat er stond zag je alleen door het projectbestand te openen.
Vandaar dat hier op het ANTWOORD gemeten wordt en niet op de sjabloonregels: de vraag is of
de gebruiker het op zijn scherm krijgt.

Wat hier bewaakt wordt is vooral de AFSCHERMING, want die twee soorten verschillen:

- een omgevingsvariabele is een gebruikerswaarde en kan van alles zijn, dus sinds RC-388
  staat alleen de NAAM op de kaart - een waarde komt niet meer in de HTML, ook niet in
  een afgeschermd veld; bekijken en bewerken vraagt een verse step-up.
- een alias die naar platformvariabelen VERWIJST noemt alleen waar de waarde vandaan komt.
  Dat afschermen maakt het blok waardeloos, want dan zie je nog steeds niet wat er staat.

Dat onderscheid is niet van dit sjabloon: het staat in ``AliasesService.owned_value_is_secret``
en komt in de sjabloonomgeving binnen als het filter ``is_verwijzing``. Deze test zet daar een
hek omheen, zodat het sjabloon niet stilletjes zijn eigen regel gaat verzinnen.

De bg-route rendert dezelfde sjablonen met ``opi/web/lotc_fixtures/voorbeeld-volledig.yaml``,
dat een component ``backend`` heeft met drie aliassen en dus de gegevens die hier nodig zijn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import opi.web.lotc_router as lotc_router_module
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.invite_routes import invite_router
from opi.web.lotc_router import router as lotc_router
from starlette.middleware.sessions import SessionMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

#: Wat het sjabloon neerzet als een waarde verborgen moet blijven.
GEHEIM_VELD = "c-secret-field"


@pytest.fixture(scope="module")
def componentenkaart() -> str:
    app = FastAPI()
    app.include_router(lotc_router)
    app.include_router(invite_router)
    app.add_middleware(SessionMiddleware, secret_key="test")
    return TestClient(app).get("/lotc/bg/project-tabs?tab=componenten").text


def _componentenkaart_met(monkeypatch: pytest.MonkeyPatch, can: Callable[..., bool]) -> str:
    """Zelfde route, maar met een zelf gekozen ``can()`` in plaats van de altijd-ja van de
    proefopstelling. Nodig om de rolpoort van de actiegroep (RC-392/RC-388-merge) zelf te
    toetsen: de proefopstelling zet ``can`` standaard op ``lambda *_a, **_k: True`` en kan
    dus niet laten zien dat ``mag_bewerken`` en ``mag_env_vars`` ONAFHANKELIJK beslissen.
    """
    origineel = lotc_router_module.page_data

    def met_eigen_can(slug: str) -> dict[str, object]:
        data = origineel(slug)
        if "can" in data:
            data["can"] = can
        return data

    monkeypatch.setattr(lotc_router_module, "page_data", met_eigen_can)

    app = FastAPI()
    app.include_router(lotc_router)
    app.include_router(invite_router)
    app.add_middleware(SessionMiddleware, secret_key="test")
    return TestClient(app).get("/lotc/bg/project-tabs?tab=componenten").text


def _backend_actiegroep(html: str) -> str:
    """Het stuk van de componentenkaart tussen de kop "backend" en de volgende kop
    ("frontend"), zodat een assertie hier niet toevallig op het andere component slaat.
    ``c-heading`` rendert als ``<h3>backend</h3>``, niet als een ``label=``-attribuut.
    """
    start = html.index("<h3>backend</h3>")
    eind = html.index("<h3>frontend</h3>")
    return html[start:eind]


def test_de_aliassen_staan_in_de_kaart(componentenkaart: str) -> None:
    """Naam en waarde allebei: een naam zonder waarde zegt nog steeds niets."""
    assert "Aliassen" in componentenkaart
    assert "POSTGRES_SERVER" in componentenkaart
    assert "$DATABASE_SERVER_HOST" in componentenkaart


def test_een_alias_die_verwijst_wordt_niet_afgeschermd(componentenkaart: str) -> None:
    """De reden dat het blok er is: verwijzingen moet je kunnen LEZEN.

    Gemeten op het stuk HTML rond de waarde, want elders op de kaart staan wel degelijk
    afgeschermde velden (de omgevingsvariabelen) en die mogen deze meting niet kleuren.
    """
    positie = componentenkaart.index("$DATABASE_SERVER_HOST")
    rondom = componentenkaart[positie - 400 : positie + 200]

    assert GEHEIM_VELD not in rondom, "een verwijzing werd afgeschermd en is dus onleesbaar"


def test_omgevingsvariabelen_staan_niet_op_de_kaart(componentenkaart: str) -> None:
    """Geen waarde en geen naam: de kaart heeft alleen de knop naar het step-up-venster.

    De fixture draagt `API_TOKEN` en `FEATURE_VLAG` als namen van component `backend`.
    """
    assert "API_TOKEN" not in componentenkaart
    assert "FEATURE_VLAG" not in componentenkaart
    assert "Omgevingsvariabelen (" not in componentenkaart

    positie = componentenkaart.index("Omgevingsvariabelen bewerken")
    rondom = componentenkaart[positie - 200 : positie + 400]

    assert GEHEIM_VELD not in rondom
    assert "/componenten/backend/env-vars" in rondom, (
        "de knop hoort naar het step-up-venster van dit component te wijzen"
    )


def test_alleen_bewerken_toont_geen_env_vars_knop(monkeypatch: pytest.MonkeyPatch) -> None:
    """``project.edit`` zonder ``env-vars.manage``: Bewerken en Verwijderen staan er, de
    omgevingsvariabelenknop niet. Rood te krijgen door de ``if mag_env_vars`` van de knop
    weg te halen: dan staat hij er ook bij deze ``can()``.
    """
    blok = _backend_actiegroep(_componentenkaart_met(monkeypatch, can=lambda actie, **_k: actie == "project.edit"))

    assert "Bewerken" in blok
    assert "Verwijderen" in blok
    assert "Omgevingsvariabelen bewerken" not in blok


def test_alleen_env_vars_toont_geen_bewerken_of_verwijderen(monkeypatch: pytest.MonkeyPatch) -> None:
    """``env-vars.manage`` zonder ``project.edit``: alleen de omgevingsvariabelenknop.

    Dit is precies het gat dat de mergeronde dichtte: voor de splitsing hing de hele
    actiegroep achter ``project.edit`` alleen, dus een rol met wel ``env-vars.manage`` en
    geen ``project.edit`` zag hier niets. Rood te krijgen door de knop weer onder de ene
    ``{% if mag_bewerken %}`` van de hele groep te zetten.
    """
    blok = _backend_actiegroep(_componentenkaart_met(monkeypatch, can=lambda actie, **_k: actie == "env-vars.manage"))

    assert "Omgevingsvariabelen bewerken" in blok
    assert "Bewerken" not in blok
    assert "Verwijderen" not in blok


def test_geen_van_beide_rechten_toont_geen_actiegroep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Noch ``project.edit`` noch ``env-vars.manage``: geen lege knoppenrij.

    De `{% if mag_bewerken or mag_env_vars %}` om de hele `<c-action-group>` heen bestaat
    om dit te voorkomen. Rood te krijgen door die buitenste `if` weg te halen: dan
    rendert een lege `<c-action-group>` voor een rol die geen van beide mag.
    """
    blok = _backend_actiegroep(_componentenkaart_met(monkeypatch, can=lambda *_a, **_k: False))

    # c-action-group rendert als een <div data-lotc-component="action-group">; het
    # jinja-brontag staat er na het renderen niet meer.
    assert 'data-lotc-component="action-group"' not in blok
    assert "Bewerken" not in blok
    assert "Omgevingsvariabelen bewerken" not in blok
    assert "Verwijderen" not in blok
