"""Wie /admin/diensten en zijn fragmenten mag opvragen.

Deze pagina toont gegevens over ALLE projecten heen: hoe vol elke PVC zit, hoe groot elke
database is en in welke namespace ze staan. Het menu-item is alleen voor een beheerder
zichtbaar, maar een link verbergen is presentatie en geen toegangscontrole - de URL is de
weg naar binnen.

De fragmenten zijn hier net zo belangrijk als de pagina. Ze zijn gewone URL's, en een
grendel op alleen de pagina laat precies dezelfde gegevens er via /admin/diensten/opslag
alsnog uit.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.web.router_shared_services import (
    gedeelde_diensten_databases,
    gedeelde_diensten_opslag,
    gedeelde_diensten_overzicht,
)

ROUTES = [
    pytest.param(gedeelde_diensten_overzicht, id="pagina"),
    pytest.param(gedeelde_diensten_opslag, id="fragment-opslag"),
    pytest.param(gedeelde_diensten_databases, id="fragment-databases"),
]


def _verzoek(email: str | None) -> Any:
    """Een verzoek met (of zonder) een ingelogde gebruiker."""
    user = {"email": email} if email else None
    return SimpleNamespace(state=SimpleNamespace(user=user), query_params={})


def _gebruikersdienst(admins: set[str]) -> Any:
    dienst = MagicMock()
    dienst.is_platform_admin.side_effect = lambda email: email in admins
    return dienst


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.asyncio
async def test_een_niet_beheerder_komt_er_niet_in(route: Any) -> None:
    with (
        patch("opi.services.user_service.get_user_service", return_value=_gebruikersdienst(set())),
        pytest.raises(HTTPException) as fout,
    ):
        await route(_verzoek("ontwikkelaar@rijksoverheid.nl"))

    assert fout.value.status_code == 403


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.asyncio
async def test_zonder_sessie_komt_er_niemand_in(route: Any) -> None:
    with (
        patch("opi.services.user_service.get_user_service", return_value=_gebruikersdienst(set())),
        pytest.raises(HTTPException) as fout,
    ):
        await route(_verzoek(None))

    assert fout.value.status_code == 401


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.asyncio
async def test_een_beheerder_komt_langs_de_grendel(route: Any) -> None:
    """De grendel mag geen beheerder tegenhouden.

    Het renderen zelf is hier vervangen: dat vraagt een echte Request en wordt door de
    browsertest gedekt. Wat deze test bewijst is dat de route TOT het renderen komt, en
    dus dat de 403 hierboven van de autorisatie komt en niet van iets anders.
    """
    with (
        patch(
            "opi.services.user_service.get_user_service",
            return_value=_gebruikersdienst({"beheerder@rijksoverheid.nl"}),
        ),
        patch("opi.web.router_shared_services.render", return_value="gerenderd") as render,
        patch("opi.web.router_shared_services.get_menu_items", return_value=[]),
        patch("opi.web.router_shared_services.build_lotc_admin", return_value={}),
        patch("opi.web.router_shared_services.haal_opslag", return_value=None),
        patch("opi.web.router_shared_services.haal_databases", return_value=None),
    ):
        antwoord = await route(_verzoek("beheerder@rijksoverheid.nl"))

    assert antwoord == "gerenderd"
    assert render.called
