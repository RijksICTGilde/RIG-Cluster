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

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.invite_routes import invite_router
from opi.web.lotc_router import router as lotc_router
from starlette.middleware.sessions import SessionMiddleware

#: Wat het sjabloon neerzet als een waarde verborgen moet blijven.
GEHEIM_VELD = "c-secret-field"


@pytest.fixture(scope="module")
def componentenkaart() -> str:
    app = FastAPI()
    app.include_router(lotc_router)
    app.include_router(invite_router)
    app.add_middleware(SessionMiddleware, secret_key="test")
    return TestClient(app).get("/lotc/bg/project-tabs?tab=componenten").text


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
