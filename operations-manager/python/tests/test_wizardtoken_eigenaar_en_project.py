"""Welke wizardsessie een verzoek mag openen.

Modal-wizardstate staat op schijf onder een ``_wizard_token`` in de URL. Zo'n token was
tot RC-314 zelf het toegangsbewijs: wie hem had, opende de sessie, ook met een ander
account en ook onder een ander project in het pad. Een gedeelde of uit een log geplukte
URL opende dus iemand anders' bewerking, met de projectgegevens die daarin zitten.

Wat hier bewaakt wordt, in vier lagen:

* de lader (``load_modal_state_for_user`` en ``load_modal_state_for``) - de state opent
  alleen voor de gebruiker die hem begon, en alleen onder het project waarvoor hij is
  aangemaakt;
* de routes die een token aannemen - ze gaan echt door die lader heen en geven een
  verlopen sessie in plaats van de state van een ander;
* de bijlageroutes, die geen project in hun pad hebben en de cookie-state daarom aan de
  flow van het verzoek binden;
* de afsluiting: ``get_modal_state_by_token`` is alleen nog intern in ``session.py``,
  zodat een nieuwe aanroeper de lader niet per ongeluk overslaat.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException, UploadFile
from opi.forms.wizard import session
from opi.forms.wizard.session import (
    init_modal_state_tokenized,
    load_modal_state_for,
    load_modal_state_for_user,
    save_modal_state_by_token,
    save_wizard_state,
)
from opi.forms.wizard.state import WizardState
from opi.web import router_approvals, router_detail_edit
from opi.web.router_detail_edit import _SESSION_EXPIRED
from opi.web.router_wizard_attachments import stage_attachment
from starlette.requests import Request

OWNER_EMAIL = "eigenaar@example.com"
OTHER_EMAIL = "iemand-anders@example.com"
PROJECT_NAME = "eigen-project"
OTHER_PROJECT = "ander-project"
FLOW_ID = "modal-edit-identity"

OPI_ROOT = Path(__file__).resolve().parent.parent / "opi"


@pytest.fixture(autouse=True)
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Elke test zijn eigen sessiemap, zodat de tokens van de ene de andere niet zien."""
    monkeypatch.setattr(session, "_STORE_DIR", str(tmp_path))
    return tmp_path


def _request(email: str | None = OWNER_EMAIL, token: str | None = None, flow_cookie: str | None = None) -> Request:
    """Een verzoek van *email*, met *token* in de query zoals de modal hem meestuurt."""
    query = urlencode({"_wizard_token": token}) if token else ""
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": query.encode(),
            "session": {},
            "state": {"user": {"email": email}} if email else {},
        }
    )
    if flow_cookie:
        save_wizard_state(request, WizardState(flow_id=flow_cookie, current_step="attachments"))
    return request


def _token_of(owner: str = OWNER_EMAIL, project: str = PROJECT_NAME) -> str:
    token, _ = init_modal_state_tokenized(FLOW_ID, "identity", ["identity"], project, owner)
    return token


# --------------------------------------------------------------------------------------
# 1. De lader
# --------------------------------------------------------------------------------------


def test_de_eigen_gebruiker_opent_zijn_eigen_sessie() -> None:
    token = _token_of()

    state = load_modal_state_for(_request(), token, PROJECT_NAME)

    assert state is not None
    assert state.owner_email == OWNER_EMAIL


def test_een_andere_gebruiker_opent_hem_niet() -> None:
    """Hetzelfde token, een ander ingelogd account: geen state."""
    token = _token_of()

    assert load_modal_state_for(_request(OTHER_EMAIL), token, PROJECT_NAME) is None
    assert load_modal_state_for_user(_request(OTHER_EMAIL), token) is None


def test_onder_een_ander_project_in_het_pad_opent_hij_niet() -> None:
    """De eigenaar klopt, maar het pad wijst een ander project aan dan de state draagt."""
    token = _token_of()

    assert load_modal_state_for(_request(), token, OTHER_PROJECT) is None


def test_een_state_zonder_eigenaar_geldt_als_verlopen() -> None:
    """Een bestand van voor dit veld is van niemand, en dus niet van iedereen."""
    token = "a" * 32
    save_modal_state_by_token(token, WizardState(flow_id=FLOW_ID, current_step="identity", project_name=PROJECT_NAME))

    assert load_modal_state_for(_request(), token, PROJECT_NAME) is None


def test_de_letterkast_van_het_e_mailadres_doet_niet_mee() -> None:
    """Keycloak levert niet elke keer dezelfde letterkast; dat mag geen sessie wegnemen."""
    token = _token_of("Eigenaar@Example.com")

    assert load_modal_state_for(_request("eigenaar@EXAMPLE.com"), token, PROJECT_NAME) is not None


def test_zonder_ingelogde_gebruiker_opent_hij_niet() -> None:
    token = _token_of()

    assert load_modal_state_for(_request(None), token, PROJECT_NAME) is None


# --------------------------------------------------------------------------------------
# 2. De routes die een token aannemen
# --------------------------------------------------------------------------------------


async def _load_step(request: Request, project_name: str, captured: dict[str, Any]) -> None:
    """``modal_wizard_load_step`` met alleen het renderwerk eromheen weggenomen.

    De state die de route opende komt in *captured* terecht, want wat hier gemeten wordt
    is of ze er een kreeg en welke.
    """

    def _capture(_request: Request, _token: str | None, state: WizardState, *_rest: Any) -> str:
        captured["state"] = state
        return ""

    with (
        patch.object(router_detail_edit, "authorize"),
        patch.object(router_detail_edit, "get_flow", return_value=MagicMock()),
        patch.object(router_detail_edit, "_get_section_from_flow", return_value=MagicMock()),
        patch.object(router_detail_edit, "_render_section_html", return_value=""),
        patch.object(router_detail_edit, "_render_modal_step", side_effect=_capture),
    ):
        await router_detail_edit.modal_wizard_load_step(request, project_name, FLOW_ID, "identity")


async def test_de_route_geeft_een_andere_gebruiker_een_verlopen_sessie() -> None:
    token = _token_of()

    with pytest.raises(HTTPException) as exc:
        await _load_step(_request(OTHER_EMAIL, token), PROJECT_NAME, {})

    assert exc.value.status_code == 400
    assert exc.value.detail == _SESSION_EXPIRED


async def test_de_route_geeft_hetzelfde_token_onder_een_ander_project_een_verlopen_sessie() -> None:
    token = _token_of()

    with pytest.raises(HTTPException) as exc:
        await _load_step(_request(OWNER_EMAIL, token), OTHER_PROJECT, {})

    assert exc.value.status_code == 400
    assert exc.value.detail == _SESSION_EXPIRED


async def test_de_route_opent_de_sessie_voor_de_eigen_gebruiker() -> None:
    token = _token_of()
    captured: dict[str, Any] = {}

    await _load_step(_request(OWNER_EMAIL, token), PROJECT_NAME, captured)

    assert captured["state"].owner_email == OWNER_EMAIL


async def test_de_aanvragenmodal_gaat_door_dezelfde_lader() -> None:
    """De tweede router met modal-wizardroutes onder ``{project_name}``."""
    token, _ = init_modal_state_tokenized(router_approvals.FLOW_ID, "approval", ["approval"], PROJECT_NAME, OWNER_EMAIL)

    for request, project_name in (
        (_request(OTHER_EMAIL, token), PROJECT_NAME),
        (_request(OWNER_EMAIL, token), OTHER_PROJECT),
    ):
        with (
            patch.object(router_approvals, "require_platform_admin", return_value={"email": OTHER_EMAIL}),
            pytest.raises(HTTPException) as exc,
        ):
            await router_approvals.modal_wizard_submit_step(request, project_name, router_approvals.FLOW_ID, "approval")
        assert exc.value.status_code == 400
        assert "verlopen" in exc.value.detail


# --------------------------------------------------------------------------------------
# 3. De bijlageroutes: geen project in het pad, dus de flow bindt de cookie-state
# --------------------------------------------------------------------------------------


async def _stage(request: Request, flow_id: str, token: str | None = None) -> str:
    response = await stage_attachment(
        request,
        flow_id,
        attachment_id="ca",
        file=UploadFile(file=io.BytesIO(b"inhoud"), filename="ca.pem"),
        wizard_token=token,
        mode="",
    )
    return response.body.decode()


async def test_een_bijlage_stagen_op_een_andere_flow_dan_de_sessie_wordt_geweigerd() -> None:
    """De cookie-state van een andere wizard is niet de sessie van dit verzoek."""
    request = _request(flow_cookie="create-project")

    with pytest.raises(HTTPException) as exc:
        await _stage(request, "modal-edit-attachments")

    assert exc.value.status_code == 400
    assert exc.value.detail == "Geen actieve wizard-sessie"


async def test_een_bijlage_stagen_op_de_eigen_flow_komt_er_wel_door() -> None:
    request = _request(flow_cookie="create-project")

    body = await _stage(request, "create-project")

    assert "ca.pem" in body


async def test_een_cookie_sessie_van_een_andere_flow_duwt_het_token_niet_weg() -> None:
    """Een volle wizard in een tweede tabblad mag de modal niet overnemen.

    De cookie-state werd eerder altijd eerst gekozen, dus wie beide open had stageerde zijn
    bijlage in de verkeerde sessie. Nu valt een cookie van een andere flow door naar het
    token.
    """
    token = _token_of()
    request = _request(OWNER_EMAIL, token, flow_cookie="create-project")

    body = await _stage(request, FLOW_ID, token)

    assert "ca.pem" in body
    assert load_modal_state_for(_request(), token, PROJECT_NAME).staged_attachments.keys() == {"ca"}


# --------------------------------------------------------------------------------------
# 4. De lader is niet te omzeilen
# --------------------------------------------------------------------------------------


def _calls_to(name: str, path: Path) -> list[int]:
    """Aanroepen van *name* in *path*, zowel kaal als via de module (``session.name(...)``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


def test_get_modal_state_by_token_wordt_alleen_in_session_py_aangeroepen() -> None:
    """De lader toetst eigenaar en project; de rauwe variant zou dat overslaan.

    Een AST-toets en geen afspraak op papier, omdat een nieuwe route die hem importeert
    niemand opvalt: hij werkt, en laat alleen de controle weg.
    """
    eigen = (OPI_ROOT / "forms/wizard/session.py").resolve()
    overtredingen = [
        f"{path.relative_to(OPI_ROOT)}:{lineno}"
        for path in OPI_ROOT.rglob("*.py")
        if path.resolve() != eigen
        for lineno in _calls_to("get_modal_state_by_token", path)
    ]

    assert not overtredingen, (
        "gebruik load_modal_state_for(request, token, project_name), of "
        f"load_modal_state_for_user zonder project in het pad: {overtredingen}"
    )
