"""De stand ``verify`` bestaat niet meer, en een projectbestand dat hem droeg werkt nog.

``AccountLink`` kende ``automatic``, ``confirm`` en ``verify``. De eerste twee bouwen een
eigen first-broker-login-flow; ``verify`` deed niets - ``keycloak_yaml_handler`` vertakt
alleen op de eerste twee en laat de rest op Keycloaks eigen flow staan. Wie ``verify``
koos kreeg dus exact hetzelfde als wie niets koos, terwijl de keuzelijst deed alsof er een
derde mogelijkheid was.

Het weghalen zelf is klein. Het GEVAARLIJKE deel is wat er met bestaande projectbestanden
gebeurt: een waarde die niet meer valideert blokkeert elke volgende verwerking van dat
project, en dat faalt stil - niemand kijkt naar een project dat prima stond te draaien.
Gekozen is daarom voor lezen-als-niets in plaats van afkeuren, en die keuze is hier de
poort. Zie ``features/keycloak-auto-link.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from opi.forms.visualizers.providers import KeycloakAccountLinkOptionsProvider
from opi.services.catalog.keycloak.config_model import AccountLink, KeycloakConfig

FRAGMENT = Path(__file__).resolve().parent.parent / "opi" / "services" / "catalog" / "keycloak" / "keycloak.v1.0.json"


def test_de_enum_kent_hem_niet_meer() -> None:
    assert {stand.value for stand in AccountLink} == {"automatic", "confirm"}


def test_het_schema_kent_hem_niet_meer() -> None:
    """Het vastgelegde fragment loopt anders uit de pas met het model."""
    fragment = json.loads(FRAGMENT.read_text())

    assert fragment["$defs"]["AccountLink"]["enum"] == ["automatic", "confirm"]


def test_de_keuzelijst_biedt_hem_niet_meer_aan() -> None:
    waarden = {optie["value"] for optie in KeycloakAccountLinkOptionsProvider().get_options()}

    assert "verify" not in waarden
    # de lege keuze blijft: dat IS de weg die verify beschreef
    assert "" in waarden
    assert waarden == {"", "automatic", "confirm"}


def test_een_bestaand_bestand_met_verify_valideert_nog() -> None:
    """De poort van dit punt: geen ValidationError, en de uitkomst is de stock-flow."""
    config = KeycloakConfig.model_validate({"template": "sso-only", "account-link": "verify"})

    assert config.account_link is None


def test_de_twee_standen_die_wel_iets_doen_komen_door() -> None:
    """Bewaak de bewaker: alles op None zetten zou de test hierboven ook groen maken."""
    for stand in ("automatic", "confirm"):
        config = KeycloakConfig.model_validate({"template": "sso-only", "account-link": stand})
        assert config.account_link == stand


def test_een_onbekende_stand_wordt_nog_steeds_afgekeurd() -> None:
    """Doorlaten geldt alleen voor de waarde die wij zelf hebben weggehaald."""
    with pytest.raises(ValueError, match=r"account.link"):
        KeycloakConfig.model_validate({"template": "sso-only", "account-link": "silent"})
