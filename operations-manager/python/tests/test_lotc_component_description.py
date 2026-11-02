"""The component `description` on the detail and components-tab cards (RC-402).

Measured on the rendered HTML, like test_lotc_componentkaart_waarden.py: the question is
whether a visitor sees it, not whether the template line exists. The fixture's `backend`
component carries a description, `frontend` does not, so the same page proves both the
positive and the negative case.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.invite_routes import invite_router
from opi.web.lotc_router import router as lotc_router
from starlette.middleware.sessions import SessionMiddleware

BESCHRIJVING = "API voor de zoekfunctie, leest uit de gedeelde database"


@pytest.fixture(scope="module")
def componentenkaart() -> str:
    app = FastAPI()
    app.include_router(lotc_router)
    app.include_router(invite_router)
    app.add_middleware(SessionMiddleware, secret_key="test")
    return TestClient(app).get("/lotc/bg/project-tabs?tab=componenten").text


@pytest.fixture(scope="module")
def projectdetails() -> str:
    app = FastAPI()
    app.include_router(lotc_router)
    app.include_router(invite_router)
    app.add_middleware(SessionMiddleware, secret_key="test")
    return TestClient(app).get("/lotc/bg/project-details").text


def test_de_omschrijving_staat_op_de_componentenkaart(componentenkaart: str) -> None:
    assert BESCHRIJVING in componentenkaart


def test_een_component_zonder_omschrijving_toont_niets_extra(componentenkaart: str) -> None:
    """frontend heeft geen `description`. Direct na de typeregel ("Frontend") komt op
    zijn kaart meteen de poortenregel; een component MET omschrijving (backend) heeft
    daar een extra alinea tussen. Die extra alinea moet voor frontend ontbreken."""
    na_type = componentenkaart.index("<p>Frontend</p>")
    volgende_alinea = componentenkaart.index("<p>", na_type + len("<p>Frontend</p>"))
    assert componentenkaart[volgende_alinea : volgende_alinea + 20].startswith("<p><b>Poorten:</b>")


def test_de_omschrijving_staat_op_de_projectdetailkaart(projectdetails: str) -> None:
    assert BESCHRIJVING in projectdetails
