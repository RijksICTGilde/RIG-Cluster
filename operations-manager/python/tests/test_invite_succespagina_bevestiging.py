"""De succespagina van een uitnodiging gaat over BEVESTIGEN, niet over toegang (RC-191).

Wie zich via een uitnodiging registreert kiest zijn wachtwoord en is daarna nog niet
binnen: hij moet eerst het adres bevestigen waar de mail heen ging. De pagina zei "Je hebt
nu toegang tot X" en noemde die mail met geen woord.

Dezelfde pagina dient ook de SSO-weg (``invite_routes.py`` callback), en die gebruiker komt
via ``trustEmail`` al geverifieerd binnen. De nieuwe tekst hangt daarom aan ``verify_email``,
een vlag die alleen de LOKALE tak in ``request.session["invite_success"]`` zet.
"""

from typing import Any

import pytest
from opi.core.templates_lotc import templates_lotc

TEMPLATE = "bg/invite-success.html.j2"


def _render(**overrides: Any) -> str:
    context: dict[str, Any] = {
        "request": None,
        "project_name": "demo",
        "display_name": "Demo-applicatie",
        "success_title": "Gelukt",
        "success_button": "Ga naar applicatie",
        "application_url": "https://demo.example.org",
        "language": "nl",
        "email": "iemand@example.org",
        "created": True,
        "assigned": {},
        "verify_email": False,
        "verification_mail_sent": False,
        "contact_email": "",
    }
    context.update(overrides)
    return templates_lotc.env.get_template(TEMPLATE).render(**context)


@pytest.mark.parametrize(
    ("language", "verwacht", "weg"),
    [
        ("nl", "Bevestig eerst je e-mailadres", "Je hebt nu toegang tot"),
        ("en", "Confirm your email address first", "You now have access to"),
    ],
)
def test_na_een_lokale_registratie_gaat_de_pagina_over_bevestigen(language: str, verwacht: str, weg: str) -> None:
    html = _render(language=language, verify_email=True, verification_mail_sent=True)

    assert verwacht in html
    assert weg not in html


@pytest.mark.parametrize("language", ["nl", "en"])
def test_de_pagina_noemt_het_adres_waar_de_mail_heen_ging(language: str) -> None:
    html = _render(language=language, verify_email=True, verification_mail_sent=True)

    assert "iemand@example.org" in html


@pytest.mark.parametrize(
    ("language", "verwacht"),
    [
        ("nl", "pas daarna kun je inloggen"),
        ("en", "you can only log in"),
    ],
)
def test_de_pagina_zegt_dat_inloggen_pas_daarna_werkt(language: str, verwacht: str) -> None:
    html = _render(language=language, verify_email=True, verification_mail_sent=True)

    assert verwacht in html


def test_de_knop_naar_de_applicatie_blijft_staan() -> None:
    """Daar moet hij NA de bevestiging heen."""
    html = _render(verify_email=True, verification_mail_sent=True)

    assert "https://demo.example.org" in html
    assert "Ga naar applicatie" in html


@pytest.mark.parametrize(
    ("language", "verwacht"),
    [
        ("nl", "De bevestigingsmail is niet verstuurd"),
        ("en", "The confirmation email was not sent"),
    ],
)
def test_een_mislukte_mail_krijgt_een_eigen_regel(language: str, verwacht: str) -> None:
    html = _render(language=language, verify_email=True, verification_mail_sent=False)

    assert verwacht in html


def test_de_mislukte_mail_wijst_naar_het_contactadres_van_de_uitnodiging() -> None:
    html = _render(verify_email=True, verification_mail_sent=False, contact_email="beheer@example.org")

    assert "beheer@example.org" in html


def test_zonder_contactadres_blijft_de_regel_leesbaar() -> None:
    html = _render(verify_email=True, verification_mail_sent=False, contact_email="")

    assert "de beheerder" in html


@pytest.mark.parametrize(
    ("language", "verwacht"),
    [
        ("nl", "Je hebt nu toegang tot Demo-applicatie."),
        ("en", "You now have access to Demo-applicatie."),
    ],
)
def test_na_een_sso_registratie_verandert_er_niets(language: str, verwacht: str) -> None:
    """De SSO-tak zet ``verify_email`` niet, dus die gebruiker houdt zijn oude pagina,
    inclusief de per-project instelbare ``success_title``."""
    html = _render(language=language, verify_email=False)

    assert verwacht in html
    assert "Bevestig eerst je e-mailadres" not in html
    assert "Confirm your email address first" not in html
    assert "Gelukt" in html


def test_de_waarschuwing_over_rechten_blijft_op_beide_takken_staan() -> None:
    for verify_email in (True, False):
        html = _render(verify_email=verify_email, verification_mail_sent=True, assigned={"errors": ["rol weg"]})

        assert "sommige rechten konden niet worden toegewezen" in html
