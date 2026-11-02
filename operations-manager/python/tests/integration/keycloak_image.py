"""Gedeelde hulp voor de toetsen die tegen de eigen Keycloak-image meten.

`task test-keycloak-image` bouwt de image, start hem met de startargumenten van de
basisdeployment en zet de adressen in de omgeving. Zonder die omgeving slaan de toetsen die
deze hulp gebruiken zichzelf over, zoals de andere integratietoetsen dat doen.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

TIMEOUT = 30.0


def omgeving(naam: str) -> str:
    waarde = os.environ.get(naam)
    if not waarde:
        pytest.skip(f"{naam} niet gezet, geen Keycloak-image om tegen te meten")
    return waarde


def basis_url() -> str:
    return omgeving("KEYCLOAK_IMAGE_TEST_URL").rstrip("/")


def admin_token(url: str) -> str:
    antwoord = httpx.post(
        f"{url}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": omgeving("KEYCLOAK_IMAGE_TEST_ADMIN"),
            "password": omgeving("KEYCLOAK_IMAGE_TEST_ADMIN_PASSWORD"),
        },
        timeout=TIMEOUT,
    )
    assert antwoord.status_code == 200, antwoord.text
    return antwoord.json()["access_token"]


@contextmanager
def tijdelijke_realm(url: str, token: str, naam: str, **velden: Any) -> Iterator[dict[str, str]]:
    """Maakt een realm aan en haalt hem daarna weg, ook als de toets faalt.

    Levert de autorisatieheader op, want elke aanroep binnen het blok heeft hem nodig.
    """
    headers = {"Authorization": f"Bearer {token}"}
    realms = f"{url}/admin/realms"
    httpx.delete(f"{realms}/{naam}", headers=headers, timeout=TIMEOUT)
    gemaakt = httpx.post(realms, headers=headers, json={"realm": naam, "enabled": True, **velden}, timeout=TIMEOUT)
    assert gemaakt.status_code == 201, gemaakt.text
    try:
        yield headers
    finally:
        httpx.delete(f"{realms}/{naam}", headers=headers, timeout=TIMEOUT)
