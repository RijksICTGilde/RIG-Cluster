"""``application-url`` is een https-adres, op elke schrijfweg (RC-409).

De knop op de succespagina draagt deze waarde in zijn ``href``, en de afgedwongen CSP
staat ``'unsafe-inline'`` toe: een ``javascript:``-adres daar is script op de ZAD-origin.
Het veld had geen validator en de schema's zeiden alleen ``type: string``, dus de API- en
CLI-weg lieten elk schema door. Het portaal-editable schrijft ``application-target`` en
kwam dus niet langs deze waarde.

De render van de succespagina staat in ``test_invite_bestemming_meebewegend.py``, bij de
andere knoptoetsen.
"""

from __future__ import annotations

import pytest
from opi.manager.project_validation import validation_reasons
from opi.services.catalog.invite.config_model import HTTPS_URL_PATTERN, InviteConfig
from opi.services.catalog.invite.destination import resolve_invite_url
from pydantic import ValidationError

SLEUTEL = "een-sleutel"

GEWEIGERD = [
    "javascript:alert(1)",
    "http://ergens.nl",
    "//ergens.nl",
    "data:text/html,<script>alert(1)</script>",
    # Hoofdletters worden geweigerd en niet genormaliseerd: een pattern in JSON Schema is
    # hoofdlettergevoelig, dus normaliseren zou de twee poorten uit elkaar laten lopen.
    "HTTPS://ergens.nl",
    "https:///geen-host",
    "/relatief/pad",
]


def _entry(**extra: str) -> dict[str, object]:
    return {"key": SLEUTEL, **extra}


@pytest.mark.parametrize("adres", GEWEIGERD)
def test_een_adres_dat_geen_https_is_wordt_geweigerd(adres: str) -> None:
    with pytest.raises(ValidationError) as fout:
        InviteConfig.model_validate({"active": [_entry(**{"application-url": adres})]})

    # Door dezelfde trechter als een gebruiker hem ziet: die laat pydantic's
    # ``input_value`` weg, want een afgekeurde waarde kan een geheim zijn.
    melding = validation_reasons(fout.value)
    assert "https://" in melding
    assert adres not in melding


@pytest.mark.parametrize("adres", ["https://ergens.anders.nl/", "https://host:8443/pad?q=1"])
def test_een_https_adres_blijft_werken(adres: str) -> None:
    config = InviteConfig.model_validate({"active": [_entry(**{"application-url": adres})]})

    assert config.active[0].application_url == adres


def test_een_leeg_veld_blijft_leeg() -> None:
    """Geen bestemming is een geldige keuze: dan staat er geen knop."""
    config = InviteConfig.model_validate({"active": [_entry()]})

    assert config.active[0].application_url is None


@pytest.mark.parametrize("adres", GEWEIGERD)
def test_resolve_geeft_geen_adres_voor_een_bestand_dat_erlangs_kwam(adres: str) -> None:
    """De tweede laag, voor een bestand dat ooit buiten de validator om is geschreven."""
    assert resolve_invite_url({"application_url": adres}, {}) == ""


def test_het_pattern_staat_in_het_schemafragment() -> None:
    """Een client die het fragment leest ziet dezelfde regel als de validator handhaaft."""
    from opi.services.catalog.invite.config_model import InviteEntry

    veld = InviteEntry.model_json_schema()["properties"]["application-url"]
    assert veld["pattern"] == HTTPS_URL_PATTERN
