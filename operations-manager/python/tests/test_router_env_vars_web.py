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
from opi.http.auth.authz import enforce_route_action
from opi.http.auth.step_up_dialog import MODAL_TARGET
from opi.services.project_service import ProjectUser
from opi.services.step_up import STEP_UP_SESSION_KEY, STEP_UP_WINDOW
from opi.web.router_env_vars import save_env_vars, show_env_vars_window

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
        scope={},
        path_params={},
        query_params={},
    )


async def _venster(request: Any, component: str, deployment: str | None = None) -> Any:
    """De poort en daarna de handler, zoals de router ze achter elkaar zet.

    De handler weegt sinds RC-398 niets meer zelf: hij leest het project uit de
    beslissing van de poort, en die staat hier dus voor hem.
    """
    await _poort(request, show_env_vars_window, component, deployment)
    return await show_env_vars_window(request, PROJECT, component, deployment)


async def _opslaan(request: Any, component: str, deployment: str | None = None) -> Any:
    """Idem voor de opslagroute."""
    await _poort(request, save_env_vars, component, deployment)
    return await save_env_vars(request, PROJECT, component, deployment)


async def _poort(request: Any, endpoint: Any, component: str, deployment: str | None) -> None:
    """``enforce_route_action`` zoals de router hem als dependency ophangt.

    De deployment komt op de componentroute uit de QUERY, net als in de browser: de poort
    hoort daar dezelfde resource te wegen als de handeling raakt.
    """
    request.scope = {"route": SimpleNamespace(endpoint=endpoint)}
    request.path_params = {"project_name": PROJECT, "component_name": component}
    request.query_params = {"deployment_name": deployment} if deployment else {}
    await enforce_route_action(request)


def _access_patch():
    """De rolpoort staat open; de store levert het vaste project.

    Sinds RC-392 beslist ``opi.http.auth.authz``, dus daar staan de twee lezingen die de poort
    voedt: het projectbestand en of de aanroeper platformbeheerder is.
    """
    return patch.multiple(
        "opi.http.auth.authz",
        get_project_store=lambda: SimpleNamespace(
            get=lambda _name: SimpleNamespace(name=PROJECT, data=PROJECT_DATA, users=[])
        ),
        get_user_service=lambda: SimpleNamespace(is_platform_admin=lambda _e: True),
    )


# --- de weigering ----------------------------------------------------------


async def test_opening_without_step_up_returns_the_dialog_and_no_value() -> None:
    with (
        _access_patch(),
        patch("opi.http.auth.step_up_dialog.render_step_up_dialog", AsyncMock(return_value="<dialoog>")) as dialoog,
    ):
        antwoord = await _venster(_request(step_up=False), "web")

    assert antwoord.headers["HX-Retarget"] == MODAL_TARGET
    assert dialoog.await_args is not None
    assert dialoog.await_args.kwargs["method"] == "GET"
    # De weigering bevat nooit een waarde: de dialoog kreeg geen projectdata te zien.
    assert dialoog.await_args.kwargs.get("vervolg_velden") in (None, {})


async def test_opening_with_an_expired_step_up_returns_the_dialog_again() -> None:
    with (
        _access_patch(),
        patch("opi.http.auth.step_up_dialog.render_step_up_dialog", AsyncMock(return_value="<dialoog>")) as dialoog,
    ):
        antwoord = await _venster(_request(oud=True), "web")

    assert antwoord.headers["HX-Retarget"] == MODAL_TARGET
    assert dialoog.await_count == 1


async def test_saving_without_step_up_writes_nothing() -> None:
    with (
        _access_patch(),
        patch("opi.http.auth.step_up_dialog.render_step_up_dialog", AsyncMock(return_value="<dialoog>")),
        patch("opi.web.router_env_vars.ProjectManager") as pm,
    ):
        await _opslaan(_request({"vars": "A=1"}, step_up=False), "web")

    pm.assert_not_called()


async def test_saving_with_an_expired_step_up_writes_nothing() -> None:
    with (
        _access_patch(),
        patch("opi.http.auth.step_up_dialog.render_step_up_dialog", AsyncMock(return_value="<dialoog>")),
        patch("opi.web.router_env_vars.ProjectManager") as pm,
    ):
        await _opslaan(_request({"vars": "A=1"}, oud=True), "web")

    pm.assert_not_called()


async def test_without_htmx_the_refusal_is_a_plain_403() -> None:
    with _access_patch(), pytest.raises(HTTPException) as info:
        await _venster(_request(step_up=False, htmx=False), "web")

    assert info.value.status_code == 403


# --- het venster met verse step-up ------------------------------------------


async def test_the_window_shows_the_real_values() -> None:
    with (
        _access_patch(),
        patch("opi.web.router_env_vars.get_decoded_project_private_key", AsyncMock(return_value="priv")) as unwrap,
        patch("opi.web.router_env_vars.read_user_env_vars", AsyncMock(return_value={"A": "1", "B": "geheim"})) as lees,
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: repr(context)),
    ):
        antwoord = await _venster(_request(), "web")

    assert antwoord.status_code == 200
    html = antwoord.body.decode()
    unwrap.assert_awaited_once()
    lees.assert_awaited_once()
    assert lees.await_args.args[0] == "AGE-BLOK"
    assert "'vars_text': 'A=1\\nB=geheim'" in html
    assert "'fout': ''" in html
    assert "'can_write': True" in html


async def test_the_window_of_a_reader_who_may_not_save_offers_no_save() -> None:
    """RC-481: opening weighs the component, saving the project, so the two can differ.

    A developer on a project that has production may open a non-production component and
    may not write values anywhere in it. Letting them type into the field first and
    refusing at save is a dead end, and the typing happened in a secret.
    """
    data = copy.deepcopy(PROJECT_DATA)
    data["deployments"] = [
        {"name": "prod", "environment": "productie", "components": [{"reference": "web"}]},
        {"name": "test", "environment": "ontwikkeling", "components": [{"reference": "api"}]},
    ]
    with (
        patch.multiple(
            "opi.http.auth.authz",
            get_project_store=lambda: SimpleNamespace(
                get=lambda _name: SimpleNamespace(
                    name=PROJECT, data=data, users=[ProjectUser(email=EMAIL, roles=["developer"])]
                )
            ),
            get_user_service=lambda: SimpleNamespace(is_platform_admin=lambda _e: False),
        ),
        patch("opi.web.router_env_vars.get_decoded_project_private_key", AsyncMock(return_value="priv")),
        patch("opi.web.router_env_vars.read_user_env_vars", AsyncMock(return_value={"A": "1"})),
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: repr(context)),
    ):
        antwoord = await _venster(_request(), "api")

    html = antwoord.body.decode()
    assert "'vars_text': 'A=1'" in html
    assert "'can_write': False" in html


async def test_an_unreadable_block_for_a_reader_who_may_not_save_names_no_remedy() -> None:
    """The two refusals combine: an unreadable block offers no save to fix it with either.

    A reader who may save gets told opslaan vervangt the block; one who may not must not be
    pointed at an action the POST would refuse anyway.
    """
    data = copy.deepcopy(PROJECT_DATA)
    data["deployments"] = [
        {"name": "prod", "environment": "productie", "components": [{"reference": "web"}]},
        {"name": "test", "environment": "ontwikkeling", "components": [{"reference": "api"}]},
    ]
    with (
        patch.multiple(
            "opi.http.auth.authz",
            get_project_store=lambda: SimpleNamespace(
                get=lambda _name: SimpleNamespace(
                    name=PROJECT, data=data, users=[ProjectUser(email=EMAIL, roles=["developer"])]
                )
            ),
            get_user_service=lambda: SimpleNamespace(is_platform_admin=lambda _e: False),
        ),
        patch("opi.web.router_env_vars.get_decoded_project_private_key", AsyncMock(return_value="priv")),
        patch("opi.web.router_env_vars.read_user_env_vars", AsyncMock(return_value=None)),
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: repr(context)),
    ):
        antwoord = await _venster(_request(), "api")

    html = antwoord.body.decode()
    assert "'can_write': False" in html
    assert "niet te lezen" in html
    assert "Opslaan vervangt" not in html


def test_the_window_without_write_rights_renders_no_save_button() -> None:
    """The same question on the template itself: no form actions, and the field is locked."""
    from opi.http.render.templates_lotc import templates_lotc

    def _render(can_write: bool) -> str:
        return templates_lotc.env.get_template("bg/_env-vars-venster.html.j2").render(
            request=SimpleNamespace(state=SimpleNamespace(csrf_token="csrf"), path_params={}, scope={}),
            project_name=PROJECT,
            component_name="web",
            deployment_name=None,
            vars_text="A=1",
            fout="",
            can_write=can_write,
            post_url="/x",
        )

    assert "Opslaan" in _render(True)
    read_only = _render(False)
    assert "Opslaan" not in read_only
    assert "readonly" in read_only


async def test_the_window_of_an_unreadable_block_warns_and_stays_empty() -> None:
    with (
        _access_patch(),
        patch("opi.web.router_env_vars.get_decoded_project_private_key", AsyncMock(return_value="priv")),
        patch("opi.web.router_env_vars.read_user_env_vars", AsyncMock(return_value=None)),
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: repr(context)),
    ):
        antwoord = await _venster(_request(), "web")

    assert antwoord.status_code == 200
    html = antwoord.body.decode()
    assert "'vars_text': ''" in html
    assert "niet te lezen" in html


async def test_the_deployment_scope_reads_the_override() -> None:
    with (
        _access_patch(),
        patch("opi.web.router_env_vars.get_decoded_project_private_key", AsyncMock(return_value="priv")),
        patch("opi.web.router_env_vars.read_user_env_vars", AsyncMock(return_value={})) as lees,
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: repr(context)),
    ):
        antwoord = await _venster(_request(), "web", "prod")

    assert lees.await_args.args[0] == "AGE-OVERRIDE"
    html = antwoord.body.decode()
    assert "'deployment_name': 'prod'" in html
    assert "/deployments/prod/componenten/web/env-vars" in html


async def test_an_unknown_component_is_a_404() -> None:
    with (
        _access_patch(),
        pytest.raises(HTTPException) as info,
    ):
        await _venster(_request(), "bestaat-niet")

    assert info.value.status_code == 404


async def test_a_reader_without_the_edit_role_gets_a_403() -> None:
    with (
        patch.multiple(
            "opi.http.auth.authz",
            get_project_store=lambda: SimpleNamespace(
                get=lambda _name: SimpleNamespace(
                    name=PROJECT, data=PROJECT_DATA, users=[ProjectUser(email=EMAIL, roles=["viewer"])]
                )
            ),
            get_user_service=lambda: SimpleNamespace(is_platform_admin=lambda _e: False),
        ),
        pytest.raises(HTTPException) as info,
    ):
        await _venster(_request(), "web")

    assert info.value.status_code == 403


async def test_een_developer_mag_een_productiecomponent_niet_openen_via_de_query() -> None:
    """De deployment in de QUERY mag de poort niet milder maken (RC-392 securityreview).

    ``web`` draait in de productiedeployment, dus de developer-grant opent hem niet, ook
    niet als de aanroeper er een andere deployment bij zet.
    """
    data = copy.deepcopy(PROJECT_DATA)
    data["deployments"] = [
        {"name": "prod", "environment": "productie", "components": [{"reference": "web"}]},
        {"name": "test", "environment": "ontwikkeling", "components": [{"reference": "web"}]},
    ]
    with (
        patch.multiple(
            "opi.http.auth.authz",
            get_project_store=lambda: SimpleNamespace(
                get=lambda _name: SimpleNamespace(
                    name=PROJECT, data=data, users=[ProjectUser(email=EMAIL, roles=["developer"])]
                )
            ),
            get_user_service=lambda: SimpleNamespace(is_platform_admin=lambda _e: False),
        ),
        pytest.raises(HTTPException) as info,
    ):
        await _venster(_request(), "web", "test")

    assert info.value.status_code == 403


async def test_het_venster_levert_een_beslissing_per_verzoek() -> None:
    """De poort beslist, de handler leest. Twee rijen in de audit betekenen twee poorten."""
    request = _request()
    with (
        _access_patch(),
        patch("opi.web.router_env_vars.get_decoded_project_private_key", AsyncMock(return_value="priv")),
        patch("opi.web.router_env_vars.read_user_env_vars", AsyncMock(return_value={})),
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: ""),
    ):
        await _venster(request, "web", "prod")

    assert len(request.state.authz_decisions) == 1
    # De deployment komt op deze route uit de query; de poort hoort hem toch te wegen.
    assert request.state.authz_resource.path == f"project/{PROJECT}/deployment/prod/component/web"


async def test_an_unknown_deployment_is_a_404() -> None:
    """Ook de tweede 404-tak van _scope_holder: de deployment zelf bestaat niet."""
    with (
        _access_patch(),
        pytest.raises(HTTPException) as info,
    ):
        await _venster(_request(), "web", "buiten")

    assert info.value.status_code == 404


async def test_a_component_not_in_an_existing_deployment_is_a_404() -> None:
    """Deployment 'prod' kent alleen een override op 'web', niet op 'api'."""
    with (
        _access_patch(),
        pytest.raises(HTTPException) as info,
    ):
        await _venster(_request(), "api", "prod")

    assert info.value.status_code == 404


# --- opslaan met verse step-up ----------------------------------------------


async def test_a_developer_may_not_save_on_a_project_that_has_production() -> None:
    """Schrijven weegt het PROJECT, net als de API (RC-481, S5).

    Het venster woog de component: een developer mocht de variabelen van een
    niet-productiecomponent wijzigen zolang hij er zelf niet in productie stond. De
    API weegt het project, en dat is de strengere van de twee.
    """
    data = copy.deepcopy(PROJECT_DATA)
    data["deployments"] = [
        {"name": "prod", "environment": "productie", "components": [{"reference": "web"}]},
        {"name": "test", "environment": "ontwikkeling", "components": [{"reference": "api"}]},
    ]
    use_case = _use_case()
    with (
        patch.multiple(
            "opi.http.auth.authz",
            get_project_store=lambda: SimpleNamespace(
                get=lambda _name: SimpleNamespace(
                    name=PROJECT, data=data, users=[ProjectUser(email=EMAIL, roles=["developer"])]
                )
            ),
            get_user_service=lambda: SimpleNamespace(is_platform_admin=lambda _e: False),
        ),
        _save_patches(use_case, AsyncMock()),
        pytest.raises(HTTPException) as info,
    ):
        await _opslaan(_request({"vars": "A=1"}), "api")

    assert info.value.status_code == 403
    use_case.assert_not_awaited()


#: What the route hands the use case, captured per test.
def _use_case(result: Any = None, refusal: Exception | None = None) -> Any:
    """The shared values use case as the route sees it: patched, and recording its change."""
    if refusal is not None:
        return AsyncMock(side_effect=refusal)
    return AsyncMock(return_value=result or SimpleNamespace(changed=True, processing={"status": "skipped"}))


def _save_patches(use_case: Any, rollout_task: Any) -> Any:
    return patch.multiple(
        "opi.web.router_env_vars",
        ProjectManager=lambda **_: SimpleNamespace(close=AsyncMock()),
        set_component_values=use_case,
        create_task_and_render_progress=rollout_task,
    )


async def test_saving_asks_the_shared_use_case_to_replace_the_whole_set() -> None:
    """The window carries every variable, so it writes through REPLACE and nothing else."""
    use_case = _use_case()
    rollout_task = AsyncMock(return_value=SimpleNamespace(ok=True))
    with _access_patch(), _save_patches(use_case, rollout_task):
        antwoord = await _opslaan(_request({"vars": "A=1\nB=twee"}), "web")

    assert getattr(antwoord, "ok", False)
    change = use_case.await_args.args[0]
    assert change.operation.value == "replace"
    assert change.values == {"A": "1", "B": "twee"}
    assert change.service_name == "user-env-vars"
    assert change.layer.value == "component"
    assert change.deployment_name is None
    # The rollout is the task this route answers with, not something the write does.
    assert change.rollout is False


async def test_saving_rolls_out_only_the_deployments_using_the_component() -> None:
    """The scope of the task, which ``create_project`` could never narrow (RC-481, S4)."""
    use_case = _use_case()
    rollout_task = AsyncMock(return_value=SimpleNamespace(ok=True))
    with _access_patch(), _save_patches(use_case, rollout_task):
        await _opslaan(_request({"vars": "A=1"}), "web")

    assert rollout_task.await_args.kwargs["task_type"] == "rollout_project"
    assert rollout_task.await_args.kwargs["payload"] == {"project_name": PROJECT, "deployment_names": ["prod"]}


async def test_saving_in_a_deployment_scope_writes_the_override_layer() -> None:
    use_case = _use_case()
    rollout_task = AsyncMock(return_value=SimpleNamespace(ok=True))
    with _access_patch(), _save_patches(use_case, rollout_task):
        await _opslaan(_request({"vars": "A=9"}), "web", "prod")

    change = use_case.await_args.args[0]
    assert change.layer.value == "deployment-component"
    assert change.deployment_name == "prod"
    assert rollout_task.await_args.kwargs["payload"]["deployment_names"] == ["prod"]


async def test_saving_empty_asks_for_an_empty_set() -> None:
    """Empty input removes the key, which REPLACE with nothing in it is."""
    use_case = _use_case()
    rollout_task = AsyncMock(return_value=SimpleNamespace(ok=True))
    with _access_patch(), _save_patches(use_case, rollout_task):
        await _opslaan(_request({"vars": "  "}), "web")

    assert use_case.await_args.args[0].values == {}


async def test_invalid_input_rerenders_the_window_with_an_error_without_writing() -> None:
    use_case = _use_case()
    rollout_task = AsyncMock()
    with (
        _access_patch(),
        _save_patches(use_case, rollout_task),
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: repr(context)),
    ):
        antwoord = await _opslaan(_request({"vars": "geen-gelijke-teken"}), "web")

    assert antwoord.status_code == 200
    html = antwoord.body.decode()
    assert "'fout': '" in html
    assert "'vars_text': 'geen-gelijke-teken'" in html
    use_case.assert_not_awaited()
    rollout_task.assert_not_awaited()


async def test_unchanged_text_does_not_roll_out() -> None:
    """Dezelfde inhoud opnieuw opslaan mag de step-up niet in een commit veranderen."""
    use_case = _use_case(SimpleNamespace(changed=False, processing={"status": "skipped"}))
    rollout_task = AsyncMock()
    with _access_patch(), _save_patches(use_case, rollout_task):
        antwoord = await _opslaan(_request({"vars": "A=1"}), "web")

    assert antwoord.status_code == 303 or "HX-Redirect" in getattr(antwoord, "headers", {})
    rollout_task.assert_not_awaited()


async def test_an_unknown_component_is_a_404_before_anything_is_written() -> None:
    use_case = _use_case()
    with _access_patch(), _save_patches(use_case, AsyncMock()), pytest.raises(HTTPException) as info:
        await _opslaan(_request({"vars": "A=1"}), "bestaat-niet")

    assert info.value.status_code == 404
    use_case.assert_not_awaited()


async def test_a_refused_write_rerenders_the_window_and_starts_no_task() -> None:
    """Een geweigerde schrijfactie is geen halve opslag: het venster toont de fout."""
    from opi.manager.service.errors import ChangeConflictError

    use_case = _use_case(refusal=ChangeConflictError("push afgewezen, ref is anders"))
    rollout_task = AsyncMock()
    with (
        _access_patch(),
        _save_patches(use_case, rollout_task),
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: repr(context)),
    ):
        antwoord = await _opslaan(_request({"vars": "A=1"}), "web")

    assert antwoord.status_code == 200
    html = antwoord.body.decode()
    assert "push afgewezen" in html
    assert "'vars_text': 'A=1'" in html
    rollout_task.assert_not_awaited()


def test_the_repeat_of_a_get_action_is_a_get() -> None:
    """Na de code post de dialoog; het venster dat erachter zit is een GET (RC-388)."""
    from opi.http.render.templates_lotc import templates_lotc

    html = templates_lotc.env.get_template("bg/_step-up-herhalen.html.j2").render(
        request=SimpleNamespace(state=SimpleNamespace(csrf_token="csrf")),
        vervolg="/projects/bouwput/componenten/web/env-vars",
        vals="{}",
        methode="GET",
    )
    assert 'hx-get="/projects/bouwput/componenten/web/env-vars"' in html
    assert "hx-post" not in html


# --- a refused save never repeats the value ---------------------------------

#: A value nothing else in the suite uses, so a hit anywhere is this one.
PROBE_VALUE = "waarde-die-nergens-anders-voorkomt"

ENCRYPTED = "-----BEGIN AGE ENCRYPTED FILE-----\nversleuteld\n-----END AGE ENCRYPTED FILE-----\n"


def _schema_refusal() -> Exception:
    """The real refusal of a project whose env vars are stored readable.

    Through ``validate_project_schema`` rather than hand-written, so the test judges the
    message the write path actually raises.
    """
    from opi.services.project_schema import ProjectSchemaError, latest_schema_version, validate_project_schema

    data = copy.deepcopy(PROJECT_DATA)
    data["schema-version"] = str(latest_schema_version())
    data["components"][0]["user-env-vars"] = f"SECRET={PROBE_VALUE}"
    try:
        validate_project_schema(data)
    except ProjectSchemaError as refusal:
        return refusal
    raise AssertionError("a readable env var should not pass the project schema")


async def test_the_schema_refusal_itself_carries_no_value() -> None:
    refusal = _schema_refusal()

    assert PROBE_VALUE not in str(refusal)
    assert "user-env-vars" in str(refusal), "the message still has to name the field"


async def test_a_refused_save_shows_and_logs_no_value(caplog: Any) -> None:
    """The window comes back with the refusal, and neither page nor log carries the value."""
    from opi.manager.service.errors import ChangeValidationError

    use_case = _use_case(refusal=ChangeValidationError(str(_schema_refusal())))
    with (
        _access_patch(),
        _save_patches(use_case, AsyncMock()),
        patch("opi.web.router_env_vars.render_fragment", lambda _r, template, context: repr(context)),
        caplog.at_level("DEBUG"),
    ):
        response = await _opslaan(_request({"vars": f"SECRET={PROBE_VALUE}"}), "web")

    # The form keeps what was typed; the MESSAGE is what must not carry the value.
    message = response.body.decode().split("'fout': ", 1)[1]
    assert "Projectbestand" in message, message[:120]
    assert PROBE_VALUE not in message
    assert PROBE_VALUE not in caplog.text
