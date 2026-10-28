"""De ``?components=`` query op ``GET .../modal-wizard/<flow_id>`` (RC-378).

``modal_wizard_init`` (``router_detail_edit.py``) leest deze query en smalt de modal tot
die namen, via ``opi.forms.wizard.selection``. De functies daar (``parse_selection``,
``narrow_to_selection``, ``selectable_names``) worden al apart gemeten in
``tests/forms/test_component_modal_removal_and_selection.py`` en
``tests/forms/test_modal_noop_roundtrip.py``; hier gaat het om de route zelf: een flow
zonder ``selectable`` weigert de query, een onbekende naam geeft 404, en een geldige
selectie laat de sessie en de gerenderde stap alleen die componenten dragen.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from opi.forms.wizard.session import get_modal_state_by_token
from opi.services.project_service import ProjectService, ProjectUser
from opi.services.user_service import get_user_service

PROJECT_NAME = "selectieproef"
OWNER_EMAIL = "owner@example.com"

AGE_BLOB = "-----BEGIN AGE ENCRYPTED FILE-----\nfake\n-----END AGE ENCRYPTED FILE-----"

STORED_DATA: dict[str, Any] = {
    "name": PROJECT_NAME,
    "display-name": "Selectieproef",
    "clusters": ["sandboxed-local"],
    "users": [{"email": OWNER_EMAIL, "roles": ["admin", "ops"]}],
    "config": {
        "api-key": AGE_BLOB,
        "age-public-key": "age1publicpublicpublic",
        "age-private-key": AGE_BLOB,
    },
    "components": [
        {"name": "web", "type": "single", "ports": {"inbound": [80]}},
        {"name": "api", "type": "single", "ports": {"inbound": [81]}},
    ],
}


@pytest.fixture
def project_service() -> ProjectService:
    service = ProjectService()
    saved_projects = dict(service._projects)
    saved_admins = set(get_user_service()._platform_admin_emails)
    service._projects.clear()
    get_user_service()._platform_admin_emails.clear()
    service.register(
        project_name=PROJECT_NAME,
        api_key=STORED_DATA["config"]["api-key"],
        filename=f"{PROJECT_NAME}.yaml",
        users=[ProjectUser(email=OWNER_EMAIL, role="owner")],
        data=dict(STORED_DATA),
    )
    yield service
    service._projects.clear()
    service._projects.update(saved_projects)
    get_user_service()._platform_admin_emails.clear()
    get_user_service()._platform_admin_emails.update(saved_admins)


def _request(query: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user={"email": OWNER_EMAIL}), query_params=query)


def _token_from_html(html: str) -> str:
    match = re.search(r'name="_wizard_token" value="([0-9a-f]{32})"', html)
    assert match, "geen wizard-token in de gerenderde stap"
    return match.group(1)


@pytest.mark.asyncio
async def test_selectie_op_een_flow_zonder_selectable_is_400(project_service: ProjectService) -> None:
    """``modal-edit-identity`` kent geen ``selectable``; de query mag niet stil genegeerd worden."""
    from opi.web.router_detail_edit import modal_wizard_init

    with pytest.raises(HTTPException) as exc:
        await modal_wizard_init(_request({"components": "web"}), PROJECT_NAME, "modal-edit-identity")

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_onbekende_naam_in_de_selectie_is_404(project_service: ProjectService) -> None:
    from opi.web.router_detail_edit import modal_wizard_init

    with pytest.raises(HTTPException) as exc:
        await modal_wizard_init(_request({"components": "bestaat-niet"}), PROJECT_NAME, "modal-edit-components")

    assert exc.value.status_code == 404
    assert "bestaat-niet" in exc.value.detail


@pytest.mark.asyncio
async def test_geldige_selectie_smalt_de_sessie_tot_dat_component(project_service: ProjectService) -> None:
    from opi.web.router_detail_edit import modal_wizard_init

    response = await modal_wizard_init(_request({"components": "api"}), PROJECT_NAME, "modal-edit-components")

    html = response.body.decode()
    assert response.status_code == 200

    token = _token_from_html(html)
    state = get_modal_state_by_token(token)
    assert state is not None
    assert state.selection == ["api"]
    merged = state.get_merged_data()
    assert [c.get("name") for c in merged.get("components") or []] == ["api"]


@pytest.mark.asyncio
async def test_zonder_selectie_blijft_de_modal_alle_componenten_tonen(project_service: ProjectService) -> None:
    from opi.web.router_detail_edit import modal_wizard_init

    response = await modal_wizard_init(_request({}), PROJECT_NAME, "modal-edit-components")

    html = response.body.decode()
    token = _token_from_html(html)
    state = get_modal_state_by_token(token)
    assert state is not None
    assert state.selection == []
    merged = state.get_merged_data()
    assert [c.get("name") for c in merged.get("components") or []] == ["web", "api"]
