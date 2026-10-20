"""DNS-1123-vorm op ``subdomain`` en ``base_domain`` (RC-327 stap 7).

Het model toetst alleen de VORM; of een domein ondersteund of goedgekeurd is, blijft bij
``validate_base_domain`` en de goedkeuringsroute. Deze tests dekken alleen de vorm.
"""

from __future__ import annotations

import pytest
from opi.services.catalog.publish_on_web.config_model import PublishOnWebDeploymentConfig
from pydantic import ValidationError


@pytest.mark.parametrize("value", ["rijksapp.nl", "domein.nl", "sub.domein.nl"])
def test_geldige_base_domains_passeren(value: str) -> None:
    model = PublishOnWebDeploymentConfig(**{"base-domain": value})
    assert model.base_domain == value


@pytest.mark.parametrize("value", ["mijn-app"])
def test_geldige_subdomains_passeren(value: str) -> None:
    model = PublishOnWebDeploymentConfig(subdomain=value)
    assert model.subdomain == value


def test_geen_waarde_passeert() -> None:
    model = PublishOnWebDeploymentConfig()
    assert model.base_domain is None
    assert model.subdomain is None


@pytest.mark.parametrize(
    "value",
    [
        "Rijksapp.nl",  # hoofdletter, een gestored waarde moet al lowercase zijn
        "mijn_app.nl",  # underscore
        "mijn app.nl",  # spatie
        "domein.nl\n",  # regeleinde
        "a" * 64 + ".nl",  # een label van 64 tekens
        "eenlabel",  # geen punt, dat is geen domein
    ],
)
def test_ongeldige_base_domains_worden_geweigerd(value: str) -> None:
    with pytest.raises(ValidationError):
        PublishOnWebDeploymentConfig(**{"base-domain": value})


@pytest.mark.parametrize(
    "value",
    [
        "Mijn-App",  # hoofdletter
        "mijn_app",  # underscore
        "mijn app",  # spatie
        "mijn-app\n",  # regeleinde
        "a" * 64,  # 64 tekens
        "-mijn-app",  # begint met koppelteken
    ],
)
def test_ongeldige_subdomains_worden_geweigerd(value: str) -> None:
    with pytest.raises(ValidationError):
        PublishOnWebDeploymentConfig(subdomain=value)
