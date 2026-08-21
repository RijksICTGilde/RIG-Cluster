"""De no-mail-lijst is een lijst die je kunt lezen, en dat moet zo blijven.

De namen worden expliciet gedeclareerd in plaats van afgeleid. Deze tests leggen vast
wat er in die declaratie mag staan (alleen zones die we ook echt beheren) en wat de drie
records precies zeggen -- want de inhoud van een SPF- of DMARC-record is geen vrije tekst:
een typefout maakt het beleid stiller, niet strenger.
"""

from __future__ import annotations

import pytest
from opi.core.dns_config import (
    DMARC_CONTENT,
    DMARC_PREFIX,
    MANAGED_DNS_ZONES,
    NO_MAIL_NAMES,
    NO_MAIL_TTL,
    NULL_MX_CONTENT,
    SPF_CONTENT,
    no_mail_names,
)


def test_no_mail_zones_are_managed_zones() -> None:
    """Een naam kan alleen onder een zone staan die we ook echt beheren."""
    assert set(NO_MAIL_NAMES) <= set(MANAGED_DNS_ZONES)


def test_every_managed_zone_declares_its_router() -> None:
    """De router van elke beheerde zone draagt het beleid; dat is de aanleiding."""
    for zone in MANAGED_DNS_ZONES:
        assert "router" in no_mail_names(zone), f"geen no-mail-beleid op router.{zone}"


def test_apex_is_not_declared() -> None:
    """De apex staat er bewust niet in: die heeft SPF en DMARC al.

    Een null MX op de apex zou zeggen dat de hele zone geen mail aanneemt, en dat is een
    beleidskeuze over de zone in plaats van de hygiene-reparatie die deze lijst is.
    """
    for names in NO_MAIL_NAMES.values():
        assert "@" not in names
        assert "" not in names


def test_record_contents() -> None:
    """De drie records zeggen: niemand mag namens deze naam mailen, en niets neemt mail aan."""
    assert SPF_CONTENT == "v=spf1 -all"
    assert NULL_MX_CONTENT == "0 ."
    assert DMARC_CONTENT == "v=DMARC1; p=reject;"
    assert DMARC_PREFIX == "_dmarc"
    assert NO_MAIL_TTL == 3600


def test_unmanaged_zone_is_refused() -> None:
    """Een zone die we niet beheren heeft geen no-mail-namen."""
    with pytest.raises(ValueError, match="not a managed DNS zone"):
        no_mail_names("example.org")


def test_names_are_relative() -> None:
    """TransIP schrijft relatieve namen; een volledige naam zou een dubbele zone opleveren."""
    for zone, names in NO_MAIL_NAMES.items():
        for name in names:
            assert not name.endswith(zone), f"{name} in zone {zone} is niet relatief"
            assert not name.endswith("."), f"{name} in zone {zone} is niet relatief"
