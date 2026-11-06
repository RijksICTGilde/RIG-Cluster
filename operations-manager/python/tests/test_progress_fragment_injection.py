"""The shared progress fragment is rendered once, and only about things the project has.

Two halves of the same defect, found in the security review of RC-29:

1. The fragment used to be rendered and then run through ``process_components``, which
   parses already-rendered HTML as a Jinja template. Autoescape escapes ``< > & " '``
   but not ``{{``, so any text that reached the fragment -- a step name, a success
   message, a subtask name -- was executed instead of shown. That is code execution in
   the OPI pod, reachable by anyone who may confirm an action on their own project.
2. The text in that fragment comes from the URL: the routes put the attachment id,
   component name and deployment name straight into the step name without ever checking
   them against the project, so an id the project does not have still reached the render.

The first fix is what makes it safe; the second keeps made-up names out of the task in
the first place (and gives the user a 404 at the click instead of a task that fails).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.step_up import STEP_UP_SESSION_KEY

if TYPE_CHECKING:
    from fastapi.responses import HTMLResponse

PAYLOAD = "{{ 7 * 7 }}"

#: De twee weergaven waarin dit fragment gerenderd wordt. Sinds RC-64 kiest
#: ``render_progress_fragment`` er zelf tussen, dus elke eigenschap hieronder hoort in
#: allebei te gelden: het is hetzelfde fragment, alleen een ander sjabloonbestand.
LAYOUTS = ["nldd", "roos"]


def _layout_request(layout: str) -> SimpleNamespace:
    """Het kleinste dat de schakelaar van een verzoek nodig heeft."""
    return SimpleNamespace(query_params={"layout": layout}, cookies={})


def _context(**overrides) -> dict:
    context = {
        "task_id": "t-1",
        "progress_url": "/projects/demo/task-progress/t-1",
        "progress": 0,
        "current_step": PAYLOAD,
        "tasks": [{"name": PAYLOAD, "status": "running", "subtasks": [{"name": PAYLOAD, "status": "running"}]}],
        "status": "running",
        "success_message": PAYLOAD,
        "on_complete": "location.reload()",
    }
    context.update(overrides)
    return context


# ---------------------------------------------------------------------------
# 1. rendered once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout", LAYOUTS)
def test_text_in_the_fragment_is_shown_not_executed(layout: str) -> None:
    from opi.web.task_progress import render_progress_fragment

    rendered = render_progress_fragment(_layout_request(layout), _context())

    assert "49" not in rendered
    assert rendered.count("7 * 7") == 3  # step, task name, subtask name


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_success_message_is_shown_not_executed(layout: str) -> None:
    from opi.web.task_progress import render_progress_fragment

    rendered = render_progress_fragment(_layout_request(layout), _context(status="completed"))

    assert "49" not in rendered
    assert "7 * 7" in rendered


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_error_and_subject_of_a_step_are_shown_not_executed(layout: str) -> None:
    """A failing step shows a reason and a subject, and those carry text from outside.

    Since RC-421 a git push, fetch or ls-remote that got no answer lands in ``error``,
    with the repository or deployment name in ``subject``. Both end up in an attribute,
    so the escaping has to hold there too, not only in the step name.
    """
    from opi.web.task_progress import render_progress_fragment

    payload = f'{PAYLOAD}<script>alert(1)</script>" onmouseover="alert(2)'
    rendered = render_progress_fragment(
        _layout_request(layout),
        _context(
            tasks=[{"name": "Manifesten naar Git pushen", "status": "failed", "subject": payload, "error": payload}],
            current_step="Manifesten naar Git pushen",
        ),
    )

    assert "49" not in rendered
    assert "<script>" not in rendered
    assert ' onmouseover="alert(2)' not in rendered


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_single_render_still_resolves_every_component(layout: str) -> None:
    """Dropping the second pass may not leave raw ``<c-...>`` tags in the answer.

    The extension replaces them when the template file is compiled, which is why the
    second pass was never needed here; this is the measurement of that claim.
    """
    from opi.web.task_progress import render_progress_fragment

    for status in ("running", "completed", "failed"):
        rendered = render_progress_fragment(_layout_request(layout), _context(status=status, current_step="stap"))
        assert "<c-" not in rendered, f"unresolved component tag with status={status}"


def _wizard_progress_context(**overrides) -> dict:
    context = {
        "task_id": "t-1",
        "project_name": "demo",
        "progress": 0,
        "current_step": PAYLOAD,
        "tasks": [{"name": PAYLOAD, "status": "running", "subtasks": [{"name": PAYLOAD, "status": "running"}]}],
        "status": "running",
        "error": None,
    }
    context.update(overrides)
    return context


def test_the_wizard_progress_modal_shows_text_instead_of_executing_it() -> None:
    """The wizard modal renders the same kind of fragment and had the same second pass.

    A step name carries user text -- a deployment or component name from the project
    file ends up in it -- so running the rendered HTML through ``process_components``
    again executed that text as Jinja.
    """
    from opi.core.templates_lotc import templates_lotc

    rendered = templates_lotc.get_template("wizard/modal_wizard_progress_fragment.html.j2").render(
        _wizard_progress_context()
    )

    assert "49" not in rendered
    assert "<c-" not in rendered, "single render must resolve every component tag"


# ---------------------------------------------------------------------------
# 2. only about things the project has
# ---------------------------------------------------------------------------


def _request(email: str = "admin@example.com") -> MagicMock:
    """Een verzoek met een VERSE step-up: de verwijderroutes vragen er een.

    Deze toetsen gaan over de 404 op een onbekend onderdeel, niet over de tweede factor;
    die staat in ``tests/test_delete_routes_step_up.py``. Het pad moet een echte string
    zijn, want de decorator kiest op ``/api/`` tussen de dialoog en het problem-detail.
    """
    request = MagicMock()
    request.state.user = {"email": email}
    request.session = {STEP_UP_SESSION_KEY: datetime.now(UTC).isoformat()}
    request.url.path = "/projects/demo/delete-component/x"
    return request


def _project(data: dict) -> MagicMock:
    project = MagicMock()
    project.data = data
    return project


def _store(data: dict) -> MagicMock:
    store = MagicMock()
    store.get.return_value = _project(data)
    return store


PROJECT = {
    "name": "demo",
    "components": [{"name": "web"}],
    "deployments": [{"name": "prod"}],
    "services": [{"attachments": {"data": [{"id": "a1", "filename": "logo.png"}]}}],
}


async def test_deleting_a_component_the_project_does_not_have_is_a_404() -> None:
    from opi.web import router

    with (
        patch.object(router, "get_project_store", return_value=_store(PROJECT)),
        patch.object(router, "is_user_authorized_for_project", return_value=True),
        patch.object(router, "get_user_roles_for_project", return_value=["admin", "ops"]),
        patch.object(router, "create_task_and_render_progress", new=AsyncMock()) as create,
        pytest.raises(HTTPException) as excinfo,
    ):
        await router.delete_component_web(_request(), "demo", PAYLOAD)

    assert excinfo.value.status_code == 404
    create.assert_not_awaited()


async def test_reprocessing_a_deployment_the_project_does_not_have_is_a_404() -> None:
    from opi.web import router

    with (
        patch.object(router, "get_project_store", return_value=_store(PROJECT)),
        patch.object(router, "is_user_authorized_for_project", return_value=True),
        patch.object(router, "get_user_roles_for_project", return_value=["admin", "ops"]),
        patch.object(router, "create_task_and_render_progress", new=AsyncMock()) as create,
        pytest.raises(HTTPException) as excinfo,
    ):
        await router.refresh_deployment_web(_request(), "demo", PAYLOAD)

    assert excinfo.value.status_code == 404
    create.assert_not_awaited()


async def test_deleting_an_attachment_the_project_does_not_have_is_a_404() -> None:
    from opi.web import router_attachments

    with (
        patch.object(router_attachments, "authorized_project", return_value=_project(PROJECT)),
        patch.object(router_attachments, "create_task_and_render_progress", new=AsyncMock()) as create,
        pytest.raises(HTTPException) as excinfo,
    ):
        await router_attachments.delete_attachment(_request(), "demo", PAYLOAD)

    assert excinfo.value.status_code == 404
    create.assert_not_awaited()


async def test_an_attachment_the_project_does_have_starts_the_task() -> None:
    from opi.web import router_attachments

    with (
        patch.object(router_attachments, "authorized_project", return_value=_project(PROJECT)),
        patch.object(router_attachments, "create_task_and_render_progress", new=AsyncMock()) as create,
    ):
        await router_attachments.delete_attachment(_request(), "demo", "a1")

    create.assert_awaited_once()


# ---------------------------------------------------------------------------
# the progress fragment is scoped to its project
# ---------------------------------------------------------------------------


async def test_a_task_of_another_project_is_not_readable_through_this_project() -> None:
    from opi.web import router

    task_service = MagicMock()
    task_service.get_task = AsyncMock(return_value={"task_id": "t-1", "project_name": "other"})

    with patch.object(router, "is_user_authorized_for_project", return_value=True):
        task = await router._require_task_of_project(_request(), task_service, "demo", "t-1")

    assert task is None


async def test_a_project_you_may_not_see_gives_403_on_task_progress() -> None:
    from opi.web import router

    task_service = MagicMock()
    task_service.get_task = AsyncMock(return_value={"task_id": "t-1", "project_name": "demo"})

    with (
        patch.object(router, "is_user_authorized_for_project", return_value=False),
        pytest.raises(HTTPException) as excinfo,
    ):
        await router._require_task_of_project(_request(), task_service, "demo", "t-1")

    assert excinfo.value.status_code == 403


async def test_whoever_started_the_task_may_follow_it_to_the_end() -> None:
    """A delete removes the project from the store, so the poll that reports "klaar"
    arrives when the user is no longer a member of anything. Scoping on the project
    alone would 403 exactly the task that needed watching."""
    from opi.web import router

    task_service = MagicMock()
    task_service.get_task = AsyncMock(
        return_value={"task_id": "t-1", "project_name": "demo", "created_by": "Admin@Example.com"}
    )

    with patch.object(router, "is_user_authorized_for_project", return_value=False) as authorized:
        task = await router._require_task_of_project(_request("admin@example.com"), task_service, "demo", "t-1")

    assert task is not None
    authorized.assert_not_called()


async def _modal_progress(email: str, task: dict | None, *, member: bool) -> HTMLResponse:
    """``modal_wizard_progress_html`` met alleen de taakservice weggenomen.

    De poort erachter (``_require_task_of_project``) draait echt: dat is wat gemeten wordt.
    """
    from opi.web import router
    from opi.web.router_detail_edit import modal_wizard_progress_html

    task_service = MagicMock()
    task_service.get_task = AsyncMock(return_value=task)

    with (
        patch("opi.core.task_helpers.get_task_service", return_value=task_service),
        patch.object(router, "is_user_authorized_for_project", return_value=member),
    ):
        return await modal_wizard_progress_html(_request(email), "demo", "t-1")


async def test_de_modal_geeft_een_taak_van_een_ander_project_niet_vrij() -> None:
    """Hetzelfde gat als in de zusterroute: het taak-id alleen was genoeg om mee te kijken."""
    response = await _modal_progress("lid@example.com", {"task_id": "t-1", "project_name": "ander"}, member=True)

    assert response.status_code == 404


async def test_de_modal_geeft_een_niet_lid_geen_voortgang() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await _modal_progress("vreemde@example.com", {"task_id": "t-1", "project_name": "demo"}, member=False)

    assert excinfo.value.status_code == 403


async def test_de_modal_geeft_een_lid_het_fragment() -> None:
    response = await _modal_progress(
        "lid@example.com",
        {"task_id": "t-1", "project_name": "demo", "status": "running", "current_step": "Verwerken"},
        member=True,
    )

    assert response.status_code == 200
    assert "Verwerken" in response.body.decode()


async def test_de_modal_geeft_een_onbekende_taak_een_404() -> None:
    """Hetzelfde antwoord als een taak van een ander project, zodat het niet zegt welk van de twee."""
    response = await _modal_progress("lid@example.com", None, member=True)

    assert response.status_code == 404
