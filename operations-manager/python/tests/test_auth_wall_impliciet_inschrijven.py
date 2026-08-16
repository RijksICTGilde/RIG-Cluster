"""De auth wall aan een component hangen mag hem ook op projectniveau bijschrijven.

DE MELDING (zad-cli, bevinding 21)

"authorization-wall moet eerst op projectniveau geconfigureerd zijn voor je hem kunt
binden." Een client die de dienst aan een component hing, liep vast op iets wat hij niet
kon raden: een tweede aanroep op een ander niveau, voor een dienst die hij net al gekozen
had.

DE REGEL, EN WAAROM DEZE DIENST ERBINNEN VALT

Een dienst mag zichzelf op projectniveau inschrijven zodra dat niveau geen BESLISSING
draagt. Bij de auth wall staat daar een optionele bannertekst en verder niets, dus er valt
niets te kiezen dat wij anders voor de gebruiker zouden invullen. Bij keycloak ligt dat
anders: daar kiest het projectniveau een blueprint en een realm, en dan moet de gebruiker
eerst zelf iets zeggen.

Wat blijft staan is de afhankelijkheid: zonder publish-on-web en keycloak kan een auth wall
niet werken. Dat is geen keuze maar een feit, en het hoort gemeld te worden.
"""

from __future__ import annotations

import pytest
from opi.services.catalog.base import ConfigLayer
from opi.services.registry import SERVICES
from opi.services.services_enums import ServiceType

AUTH_WALL = ServiceType.AUTHORIZATION_WALL


def test_de_auth_wall_mag_zichzelf_inschrijven() -> None:
    assert SERVICES[AUTH_WALL].allows_implicit_project_selection is True


def test_hij_schrijft_zich_in_als_kale_selectie() -> None:
    """Geen leeg configblok, want dat suggereert dat er iets ingesteld is."""
    ingeschreven = SERVICES[AUTH_WALL].implicit_project_entry()

    assert ingeschreven == AUTH_WALL.value


def test_het_projectniveau_draagt_geen_verplichte_keuze() -> None:
    """De toets die de regel draagt: er valt niets in te vullen dat verplicht is.

    Zou hier ooit een verplicht veld bijkomen, dan is impliciet inschrijven niet langer
    juist en faalt deze test -- precies op het moment dat iemand die beslissing neemt.
    """
    model = SERVICES[AUTH_WALL].config_model_for(ConfigLayer.PROJECT)
    assert model is not None

    # Valideert zonder invoer: elk veld heeft een bruikbare standaardwaarde.
    leeg = model.model_validate({})

    assert leeg.banner is None


@pytest.mark.parametrize("vereist", ["services/publish-on-web", "services/keycloak"])
def test_de_afhankelijkheden_blijven_staan(vereist: str) -> None:
    """Impliciet inschrijven neemt de eisen niet weg; het neemt alleen een raadsel weg."""
    assert vereist in (SERVICES[AUTH_WALL].definition.requires or [])


def test_keycloak_schrijft_zich_juist_niet_in() -> None:
    """De andere kant van de regel, zodat hij niet als 'altijd maar doen' gelezen wordt."""
    assert SERVICES[ServiceType.KEYCLOAK].allows_implicit_project_selection is False
