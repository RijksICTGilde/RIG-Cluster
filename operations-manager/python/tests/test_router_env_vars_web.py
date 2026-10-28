"""De webroutes van het omgevingsvariabelen-venster (RC-388).

Het venster achter ``/projects/{p}/componenten/{c}/env-vars`` (en de deployment-variant)
is de enige plek waar het portaal de waarden van ``user-env-vars`` nog leest en schrijft,
en het vraagt altijd een verse step-up - ook als de deployment in ontwikkeling staat.

Hier staat wat de browsermeetlat niet ziet: dat de weigering de OTP-dialoog teruggeeft en
geen waarde, dat de herhaling van een GET met een GET gaat, dat een opslag versleuteld
weg gaat, en dat een opslag na het vijfminutenvenster opnieuw om een code vraagt.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from opi.core.step_up import STEP_UP_SESSION_KEY, STEP_UP_WINDOW
from opi.services.project_service import ProjectUser
from opi.services.project_store import ConflictError
from opi.web.router_env_vars import save_env_vars, show_env_vars_window
from opi.web.step_up_dialog import MODAL_TARGET

pytestmark = pytest.mark.asyncio

PROJECT = "bouwput"
EMAIL = "beheerder@rijksapp.dev"

PROJECT_DATA: dict[str, Any] = {
    "name": PROJECT,
    "config": {"age-private-key": "verpakt", "age-public-key": "age1publiek"},
    "components": [
        {"name": "web", "user-env-vars": "AGE-BLOK"},
        {"name": "api"},
    ],
    "deployments": [
        {
            "name": "prod",
            "components": [{"reference": "web", "user-env-vars": "AGE-OVERRIDE"}],
        }
    ],
}


def _request(
    velden: dict[str, str] | None = None, *, step_up: bool = True, htmx: bool = True, oud: bool = False
) -> Any:
    """Een verzoek zoals de route hem ziet. ``oud`` zet een verlopen stempel."""
    stempel = datetime.now(UTC) - (STEP_UP_WINDOW + timedelta(minutes=1) if oud else timedelta(0))
    sessie = {STEP_UP_SESSION_KEY: stempel.isoformat()} if step_up else {}
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": EMAIL}, csrf_token="csrf"),
        session=sessie,
        url=SimpleNamespace(path=f"/projects/{PROJECT}/componenten/web/env-vars"),
        method="POST" if velden is not None else "GET",
        headers={"HX-Request": "true"} if htmx else {},
        form=AsyncMock(return_value=velden or {}),
    )


def _access_patch():
    """De rolpoort staat open; de store levert het vaste project.

    Sinds RC-392 beslist ``opi.web.authz``, dus daar staan de twee lezingen die de poort
    voedt: het projectbestand en of de aanroeper platformbeheerder is.
    """
    return patch.multiple(
        "opi.web.authz",
        get_project_store=lambda: SimpleNamespace(
            get=lambda _name: SimpleNamespace(name=PROJECT, data=PROJECT_DATA, users=[])
        ),
        get_user_service=lambda: SimpleNamespace(is_platform_admin=lambda _e: True),
    )


# --- de weigering ----------------------------------------------------------


async def test_opening_without_step_up_returns_the_dialog_and_no_value() -> None:
    with (
        _access_patch(),
        patch("opi.web.step_up_dialog.render_step_up_dialog", AsyncMock(return_value="<dialoog>")) as dialoog,
    ):
        antwoord = await show_env_vars_window(_request(step_up=False), PROJECT, "web")

    assert antwoord.headers["HX-Retarget"] == MODAL_TARGET
    assert dialoog.await_args is not None
    assert dialoog.await_args.kwargs["methode"] == "GET"
    # De weigering bevat nooit een waarde: de dialoog kreeg geen projectdata te zien.
    assert dialoog.await_args.kwargs.get("vervolg_velden") in (None, {})


async def test_opening_with_an_expired_step_up_returns_the_dialog_again() -> None:
    with (
        _access_patch(),
        patch("opi.web.step_up_dialog.render_step_up_dialog", AsyncMock(return_value="<dialoog>")) as dialoog,
    ):
        antwoord = await show_env_vars_window(_request(oud=True), PROJECT, "web")

    assert antwoord.headers["HX-Retarget"] == MODAL_TARGET
    assert dialoog.await_count == 1


async def test_saving_without_step_up_writes_nothing() -> None:
    with (
        _access_patch(),
        patch("opi.web.step_up_dialog.render_step_up_dialog", AsyncMock(return_value="<dialoog>")),
        patch("opi.web.router_env_vars.ProjectManager") as pm,
    ):
        await save_env_vars(_request({"vars": "A=1"}, step_up=False), PROJECT, "web")

    pm.assert_not_called()


async def test_saving_with_an_expired_step_up_writes_nothing() -> None:
    with (
        _access_patch(),
        patch("opi.web.step_up_dialog.render_step_up_dialog", AsyncMock(return_value="<dialoog>")),
        patch("opi.web.router_env_vars.ProjectManager") as pm,
    ):
        await save_env_vars(_request({"vars": "A=1"}, oud=True), PROJECT, "web")

    pm.assert_not_called()


async def test_without_htmx_the_refusal_is_a_plain_403() -> None:
    with _access_patch(), pytest.raises(HTTPException) as info:
        await show_env_vars_window(_request(step_up=False, htmx=False), PROJECT, "web")

    assert info.value.status_code == 403


# --- het venster met verse step-up ------------------------------------------


async def test_the_window_shows_the_real_values() -> None:
    with (
        _access_patch(),
        patch("opi.web.router_env_vars.unwrap_project_key", AsyncMock(return_value="priv")) as unwrap,
        patch("opi.web.router_env_vars.read_user_env_vars", AsyncMock(return_value={"A": "1", "B": "geheim"})) as lees,
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: repr(context)),
    ):
        antwoord = await show_env_vars_window(_request(), PROJECT, "web")

    assert antwoord.status_code == 200
    html = antwoord.body.decode()
    unwrap.assert_awaited_once_with("verpakt")
    lees.assert_awaited_once()
    assert lees.await_args.args[0] == "AGE-BLOK"
    assert "'vars_text': 'A=1\\nB=geheim'" in html
    assert "'fout': ''" in html


async def test_the_window_of_an_unreadable_block_warns_and_stays_empty() -> None:
    with (
        _access_patch(),
        patch("opi.web.router_env_vars.unwrap_project_key", AsyncMock(return_value="priv")),
        patch("opi.web.router_env_vars.read_user_env_vars", AsyncMock(return_value=None)),
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: repr(context)),
    ):
        antwoord = await show_env_vars_window(_request(), PROJECT, "web")

    assert antwoord.status_code == 200
    html = antwoord.body.decode()
    assert "'vars_text': ''" in html
    assert "niet te lezen" in html


async def test_the_deployment_scope_reads_the_override() -> None:
    with (
        _access_patch(),
        patch("opi.web.router_env_vars.unwrap_project_key", AsyncMock(return_value="priv")),
        patch("opi.web.router_env_vars.read_user_env_vars", AsyncMock(return_value={})) as lees,
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: repr(context)),
    ):
        antwoord = await show_env_vars_window(_request(), PROJECT, "web", "prod")

    assert lees.await_args.args[0] == "AGE-OVERRIDE"
    html = antwoord.body.decode()
    assert "'deployment_name': 'prod'" in html
    assert "/deployments/prod/componenten/web/env-vars" in html


async def test_an_unknown_component_is_a_404() -> None:
    with (
        _access_patch(),
        pytest.raises(HTTPException) as info,
    ):
        await show_env_vars_window(_request(), PROJECT, "bestaat-niet")

    assert info.value.status_code == 404


async def test_a_reader_without_the_edit_role_gets_a_403() -> None:
    with (
        patch.multiple(
            "opi.web.authz",
            get_project_store=lambda: SimpleNamespace(
                get=lambda _name: SimpleNamespace(
                    name=PROJECT, data=PROJECT_DATA, users=[ProjectUser(email=EMAIL, roles=["viewer"])]
                )
            ),
            get_user_service=lambda: SimpleNamespace(is_platform_admin=lambda _e: False),
        ),
        pytest.raises(HTTPException) as info,
    ):
        await show_env_vars_window(_request(), PROJECT, "web")

    assert info.value.status_code == 403


async def test_an_unknown_deployment_is_a_404() -> None:
    """Ook de tweede 404-tak van _scope_holder: de deployment zelf bestaat niet."""
    with (
        _access_patch(),
        pytest.raises(HTTPException) as info,
    ):
        await show_env_vars_window(_request(), PROJECT, "web", "buiten")

    assert info.value.status_code == 404


async def test_a_component_not_in_an_existing_deployment_is_a_404() -> None:
    """Deployment 'prod' kent alleen een override op 'web', niet op 'api'."""
    with (
        _access_patch(),
        pytest.raises(HTTPException) as info,
    ):
        await show_env_vars_window(_request(), PROJECT, "api", "prod")

    assert info.value.status_code == 404


# --- opslaan met verse step-up ----------------------------------------------


class _Manager:
    """De ProjectManager zoals de route hem gebruikt, zonder git."""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.saved: dict[str, Any] | None = None

    async def get_contents(self) -> dict[str, Any]:
        return self.data

    async def save_and_commit_project(self, project_data: dict[str, Any], message: str) -> None:
        self.saved = project_data

    async def close(self) -> None:
        pass


def _manager_patch(data: dict[str, Any]) -> Any:
    manager = _Manager(data)
    return manager, patch("opi.web.router_env_vars.ProjectManager", lambda **_: manager)


async def test_saving_writes_the_encrypted_block() -> None:

    data = copy.deepcopy(PROJECT_DATA)
    manager, patcher = _manager_patch(data)
    with (
        _access_patch(),
        patcher,
        patch(
            "opi.utils.age.encrypt_age_content_sync",
            return_value="-----BEGIN AGE ENCRYPTED FILE-----\nversleuteld\n-----END AGE ENCRYPTED FILE-----\n",
        ),
        patch(
            "opi.web.router_env_vars.create_task_and_render_progress", AsyncMock(return_value=SimpleNamespace(ok=True))
        ) as taak,
    ):
        antwoord = await save_env_vars(_request({"vars": "A=1\nB=twee"}), PROJECT, "web")

    assert getattr(antwoord, "ok", False)
    stored = manager.saved["components"][0]["user-env-vars"]
    assert stored.startswith("-----BEGIN AGE ENCRYPTED FILE-----"), "het opgeslagen blok is niet versleuteld"
    assert "A=1" not in stored
    taak.assert_awaited_once()
    assert taak.await_args.kwargs["payload"]["project_file_already_saved"] is True


async def test_saving_empty_removes_the_key() -> None:

    data = copy.deepcopy(PROJECT_DATA)
    manager, patcher = _manager_patch(data)
    with (
        _access_patch(),
        patcher,
        patch(
            "opi.web.router_env_vars.create_task_and_render_progress", AsyncMock(return_value=SimpleNamespace(ok=True))
        ),
    ):
        antwoord = await save_env_vars(_request({"vars": "  "}), PROJECT, "web")

    assert getattr(antwoord, "ok", False)
    assert "user-env-vars" not in manager.saved["components"][0]


async def test_invalid_input_rerenders_the_window_with_an_error_without_writing() -> None:

    data = copy.deepcopy(PROJECT_DATA)
    manager, patcher = _manager_patch(data)
    with (
        _access_patch(),
        patcher,
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: repr(context)),
    ):
        antwoord = await save_env_vars(_request({"vars": "geen-gelijke-teken"}), PROJECT, "web")

    assert antwoord.status_code == 200
    html = antwoord.body.decode()
    assert "'fout': '" in html
    assert "'vars_text': 'geen-gelijke-teken'" in html
    assert manager.saved is None


async def test_unchanged_text_does_not_reach_git() -> None:
    """Dezelfde inhoud opnieuw opslaan mag de step-up niet in een commit veranderen."""

    data = copy.deepcopy(PROJECT_DATA)
    manager, patcher = _manager_patch(data)
    with (
        _access_patch(),
        patcher,
        patch(
            "opi.web.router_env_vars.keep_existing_ciphertext_if_unchanged",
            lambda bestaand, nieuw, _ctx: bestaand,
        ),
    ):
        antwoord = await save_env_vars(_request({"vars": "A=1"}), PROJECT, "web")

    assert antwoord.status_code == 303 or "HX-Redirect" in getattr(antwoord, "headers", {})
    assert manager.saved is None


async def test_saving_in_a_deployment_scope_writes_only_the_override() -> None:
    """De deployment-variant van de POST schrijft de houder van de override, nooit het component."""

    data = copy.deepcopy(PROJECT_DATA)
    manager, patcher = _manager_patch(data)
    with (
        _access_patch(),
        patcher,
        patch(
            "opi.utils.age.encrypt_age_content_sync",
            return_value="-----BEGIN AGE ENCRYPTED FILE-----\noverschrijving\n-----END AGE ENCRYPTED FILE-----\n",
        ),
        patch(
            "opi.web.router_env_vars.create_task_and_render_progress", AsyncMock(return_value=SimpleNamespace(ok=True))
        ),
    ):
        antwoord = await save_env_vars(_request({"vars": "A=9"}), PROJECT, "web", "prod")

    assert getattr(antwoord, "ok", False)
    overschrijving = manager.saved["deployments"][0]["components"][0]["user-env-vars"]
    assert overschrijving.startswith("-----BEGIN AGE ENCRYPTED FILE-----")
    assert manager.saved["components"][0]["user-env-vars"] == "AGE-BLOK"


async def test_saving_on_a_component_without_a_block_creates_it() -> None:
    """Component 'api' heeft nog geen blok: de eerste set komt er nieuw in (geen bestaand-blok-pad)."""

    data = copy.deepcopy(PROJECT_DATA)
    manager, patcher = _manager_patch(data)
    with (
        _access_patch(),
        patcher,
        patch(
            "opi.utils.age.encrypt_age_content_sync",
            return_value="-----BEGIN AGE ENCRYPTED FILE-----\neerste\n-----END AGE ENCRYPTED FILE-----\n",
        ),
        patch(
            "opi.web.router_env_vars.create_task_and_render_progress", AsyncMock(return_value=SimpleNamespace(ok=True))
        ),
    ):
        antwoord = await save_env_vars(_request({"vars": "EERSTE=1"}), PROJECT, "api")

    assert getattr(antwoord, "ok", False)
    assert manager.saved["components"][1]["user-env-vars"].startswith("-----BEGIN AGE ENCRYPTED FILE-----")


async def test_a_git_conflict_on_save_rerenders_the_window_and_changes_nothing() -> None:
    """Een geweigerde push is geen halve opslag: het venster toont de fout, git staat leeg."""

    data = copy.deepcopy(PROJECT_DATA)

    class _ConflictManager(_Manager):
        async def get_contents(self) -> dict[str, Any]:
            return copy.deepcopy(self.data)

        async def save_and_commit_project(self, project_data: dict[str, Any], message: str) -> None:
            raise ConflictError("push afgewezen, ref is anders")

    manager = _ConflictManager(data)
    with (
        _access_patch(),
        patch("opi.web.router_env_vars.ProjectManager", lambda **_: manager),
        patch(
            "opi.utils.age.encrypt_age_content_sync",
            return_value="-----BEGIN AGE ENCRYPTED FILE-----\nversleuteld\n-----END AGE ENCRYPTED FILE-----\n",
        ),
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: repr(context)),
        patch("opi.web.router_env_vars.create_task_and_render_progress", AsyncMock()) as taak,
    ):
        antwoord = await save_env_vars(_request({"vars": "A=1"}), PROJECT, "web")

    assert antwoord.status_code == 200
    html = antwoord.body.decode()
    assert "push afgewezen" in html
    assert "'vars_text': 'A=1'" in html
    taak.assert_not_awaited()
    assert manager.data["components"][0]["user-env-vars"] == "AGE-BLOK"


def test_the_repeat_of_a_get_action_is_a_get() -> None:
    """Na de code post de dialoog; het venster dat erachter zit is een GET (RC-388)."""
    from opi.core.templates_lotc import templates_lotc

    html = templates_lotc.env.get_template("bg/_step-up-herhalen.html.j2").render(
        request=SimpleNamespace(state=SimpleNamespace(csrf_token="csrf")),
        vervolg="/projects/bouwput/componenten/web/env-vars",
        vals="{}",
        methode="GET",
    )
    assert 'hx-get="/projects/bouwput/componenten/web/env-vars"' in html
    assert "hx-post" not in html
