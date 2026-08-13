"""
Real-life sandbox E2E: concurrent mutations on the SAME project file, x5 projects.

The interesting failure mode is not two users editing two different projects -
those touch different files and cannot conflict. It is two changes racing on
ONE project file: both read the same YAML, both modify it, and a naive
read-modify-write publishes the second over the first, silently losing the
change that landed in between (a lost update).

So every round here fires several mutations at the SAME project - via the API
without waiting, and via the browser while those tasks are still in flight -
and then asserts that ALL of them are present together in the committed file.
The whole thing runs against five projects so a race that only shows up
occasionally has five chances to appear per round.

The authoritative check is always the project YAML in the Forgejo `zad-projects`
repo; the HTTP response is never trusted on its own. A final sweep asserts the
cumulative end state of all five files, which is what catches a mutation that
was quietly dropped rounds earlier.

NOT part of the standard test set (marker `reallife`). Requires a running
sandbox with YOUR build deployed (sandbox-deploy). Run with:

    task test-e2e-sandbox-reallife

Tests in this module are ordered and share module state: they must run as a
whole file, not via -k selections of individual tests. That order dependency is
the subject here and not an oversight - the final sweep can only see a lost
update from an earlier round if that round really ran before it - so the module
carries `pytest.mark.serial` and the hook in `tests/e2e/conftest.py` puts it back
in file order after pytest-randomly has shuffled.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.edit_modal import EditModalHelper
from tests.e2e.helpers.lifecycle import (
    RUNNABLE_IMAGE,
    CreatedProject,
    create_project_via_wizard,
)
from tests.e2e.helpers.wizard import _unique_project_name, veldbesturing

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    from playwright.sync_api import BrowserContext, Page
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.reallife, pytest.mark.serial]

_VERIFY_SSL = FORGEJO_VERIFY_SSL

PROJECT_COUNT = 5

# Component names allow only lowercase letters and digits.
WEB = "web"  # created by the wizard
ALPHA = "alpha"  # added concurrently with BETA
BETA = "beta"  # added concurrently with ALPHA, removed again later
GAMMA = "gamma"  # added by the API while the UI edits the same file

SECOND_DEPLOYMENT = "acceptatie"  # added late: a new deployment redeploys the whole project

ALT_IMAGE = "nginxinc/nginx-unprivileged:1.27-alpine"

# Distinct values per component so a lost update is visible as a wrong value,
# not just as a missing key.
MEM_WEB = "512Mi"
MEM_ALPHA = "384Mi"
MEM_BETA = "320Mi"

# Plaintext markers for the env-var round trip. These must end up AGE-encrypted
# in git and must NEVER appear there as plaintext.
UI_ENV_MARKER = "RL_FROM_UI=ui-value-4517"
API_ENV_MARKER = "RL_FROM_API=api-value-7391"

EXTRA_TEAM_EMAIL = "reallife-dev@sandbox.rijksapp.dev"
NEW_DESCRIPTION = "Beschrijving aangepast via UI tijdens gelijktijdige API-taak"

AGE_ARMOR = "-----BEGIN AGE ENCRYPTED FILE-----"

# A component add reprocesses the whole project (ArgoCD, namespace, secrets), so
# these are minutes, not seconds.
TASK_TIMEOUT = 600.0
GIT_TIMEOUT = 300.0
UI_PROGRESS_TIMEOUT_MS = 300_000.0
# Modal HTMX-swap wait. The default 10s is fine locally; the live sandbox under
# concurrent load (store-lock contention + ~2.5s push) needs much more headroom.
MODAL_ACTION_TIMEOUT_MS = 60_000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def reallife_projects(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[list[CreatedProject]]:
    """Create PROJECT_COUNT projects through the real wizard, teardown via API."""
    page = sandbox_context.new_page()
    projects: list[CreatedProject] = []
    try:
        for i in range(PROJECT_COUNT):
            display_name = _unique_project_name(prefix=f"rl{i}")
            project = create_project_via_wizard(
                page,
                sandbox_url,
                forgejo,
                display_name,
                user_email=SANDBOX_TEST_USER["email"],
            )
            logger.info("Created real-life project %d/%d: %s", i + 1, PROJECT_COUNT, project.name)
            projects.append(project)
        yield projects
    finally:
        page.close()
        for project in projects:
            sandbox_api.delete_project_via_api(sandbox_url, project.name, project.api_key, verify_ssl=_VERIFY_SSL)


@pytest.fixture
def ui_page(
    request: pytest.FixtureRequest,
    sandbox_context: BrowserContext,
    artifact_dir: Path,
) -> Generator[Page]:
    """Dedicated page per test for UI mutations.

    Legt bij een mislukking de pagina vast, net als ``sandbox_page`` in conftest.
    Bij een modal die niet doet wat de test verwacht wijst de schermafdruk de
    oorzaak aan waar de traceback alleen zegt dat een veld er niet was.
    """
    page = sandbox_context.new_page()
    try:
        yield page
    finally:
        if getattr(request.node, "rep_call", None) is not None and request.node.rep_call.failed:
            naam = request.node.name.replace("/", "_").replace("::", "_")
            with contextlib.suppress(PlaywrightError):
                page.screenshot(path=str(artifact_dir / f"FAILED-{naam}.png"), full_page=True)
        page.close()


# ---------------------------------------------------------------------------
# API mutations - started without waiting, so they overlap on one file
# ---------------------------------------------------------------------------


def _start_add_component(
    base_url: str,
    project: CreatedProject,
    name: str,
    *,
    env_vars: str | None = None,
) -> str:
    body: dict = {
        "name": name,
        "image": RUNNABLE_IMAGE,
        "deployment_names": [project.deployment_name],
    }
    if env_vars:
        body["env_vars"] = env_vars
    return sandbox_api.start_task(
        base_url,
        "POST",
        f"/api/v2/projects/{project.name}/components",
        project.api_key,
        body,
        verify_ssl=_VERIFY_SSL,
    )


def _start_add_deployment(base_url: str, project: CreatedProject, deployment_name: str, components: list[str]) -> str:
    """Create a second deployment, which rolls out and starts the whole project again."""
    return sandbox_api.start_task(
        base_url,
        "POST",
        f"/api/v2/projects/{project.name}/:upsert-deployment",
        project.api_key,
        {
            "deploymentName": deployment_name,
            "components": [{"reference": name, "image": RUNNABLE_IMAGE} for name in components],
        },
        verify_ssl=_VERIFY_SSL,
    )


def _start_component_patch(base_url: str, project: CreatedProject, component: str, body: dict) -> str:
    return sandbox_api.start_task(
        base_url,
        "PATCH",
        f"/api/v2/projects/{project.name}/components/{component}",
        project.api_key,
        body,
        verify_ssl=_VERIFY_SSL,
    )


def _start_image_update(base_url: str, project: CreatedProject, component: str, image: str) -> str:
    return sandbox_api.start_task(
        base_url,
        "PUT",
        f"/api/v2/projects/{project.name}/deployments/{project.deployment_name}/image",
        project.api_key,
        {"componentName": component, "newImageUrl": image},
        verify_ssl=_VERIFY_SSL,
    )


def _settle_tasks(base_url: str, tasks: list[tuple[CreatedProject, str]]) -> None:
    """Check the outcome of every fire-and-forget task, collecting all failures.

    Only an explicit task failure fails the test. A task still running when the
    timeout expires does not: these tasks also drive ArgoCD reconciliation, which
    happens AFTER the git commit this suite is actually asserting on, and its
    speed depends on cluster health rather than on the project-file mechanism.
    Still-running tasks are logged so a systematically slow cluster stays visible.

    One failing task must not hide the others: with concurrent writes it is the
    pattern across projects that tells you whether something is racy.
    """
    failures: list[str] = []
    superseded = 0
    for project, task_id in tasks:
        state, reason = sandbox_api.task_outcome(
            base_url, task_id, project.api_key, verify_ssl=_VERIFY_SSL, timeout=TASK_TIMEOUT
        )
        if state == "failed":
            failures.append(f"{project.name}: {reason}")
        elif state == "superseded":
            # Benign: a newer covering task took over this one's ArgoCD wait. The
            # project file was already verified; the newer task re-syncs the rest.
            superseded += 1
            logger.info("Task %s for '%s' was superseded by a newer task (expected under load)", task_id, project.name)
        elif state == "running":
            logger.warning(
                "Task %s for '%s' still running after %.0fs; the project file was already verified",
                task_id,
                project.name,
                TASK_TIMEOUT,
            )
    if superseded:
        logger.info("%d/%d tasks were superseded this round", superseded, len(tasks))
    assert not failures, "API tasks failed:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# UI mutations
# ---------------------------------------------------------------------------


def _edit_description_via_ui(page: Page, base_url: str, project_name: str, description: str) -> None:
    """Change the project description through the identity edit modal (save_only)."""
    modal = EditModalHelper(page, base_url, project_name, action_timeout_ms=MODAL_ACTION_TIMEOUT_MS)
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")
    modal.fill_field("description", description)
    modal.submit_step()
    modal.wait_for_success()


def _add_team_member_via_ui(page: Page, base_url: str, project_name: str, email: str) -> None:
    """Add a team member through the team edit modal (save_only)."""
    modal = EditModalHelper(page, base_url, project_name, action_timeout_ms=MODAL_ACTION_TIMEOUT_MS)
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-team", "Projectleden beheren")
    modal.sequence_add("users")
    page.locator("#modal-wizard-form [name*='email']").last.fill(email)
    modal.submit_step()
    modal.wait_for_success()


def _component_index_in_modal(page: Page, component_name: str) -> int:
    """Return the sequence index of the named component in the open components modal."""
    # Via veldbesturing() en niet via [name='...']: onder NLDD wijst een naam-selector
    # het custom element aan (<nldd-text-field>) en is input_value() daarop een harde
    # fout ("Node is not an <input>"), geen leeg veld. Zie helpers/wizard.py.
    for i in range(12):
        name_field = veldbesturing(page, f"components[{i}]/name")
        if name_field.count() == 0:
            break
        if (name_field.first.input_value() or "") == component_name:
            return i
    raise AssertionError(f"Component '{component_name}' not found in the components modal")


def _open_components_modal(page: Page, base_url: str, project_name: str) -> EditModalHelper:
    modal = EditModalHelper(page, base_url, project_name, action_timeout_ms=MODAL_ACTION_TIMEOUT_MS)
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-components", "Components beheren")
    return modal


def _await_progress(modal: EditModalHelper, project_name: str) -> None:
    """Wait for the modal's progress panel, tolerating a slow reconcile.

    These modals reprocess the whole project (manifests, ArgoCD), which happens
    after the project file is committed. The file is asserted separately, so a
    slow tail here must not fail the test - but it is logged.
    """
    try:
        modal.wait_for_progress_complete(timeout=UI_PROGRESS_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        logger.warning(
            "Modal progress for '%s' did not finish within %.0fs; verifying the project file instead",
            project_name,
            UI_PROGRESS_TIMEOUT_MS / 1000,
        )


def _set_env_vars_via_ui(page: Page, base_url: str, project_name: str, component_name: str, env_text: str) -> None:
    """Set a component's user env vars through the components modal (process_project)."""
    modal = _open_components_modal(page, base_url, project_name)
    index = _component_index_in_modal(page, component_name)
    # The env-vars field is a CodeMirror editor overlaying a hidden textarea.
    modal.fill_codemirror_kv(f"components[{index}]/user-env-vars", env_text)
    modal.submit_step_expect_progress()
    _await_progress(modal, project_name)
    modal.close_modal()


def _remove_component_via_ui(page: Page, base_url: str, project_name: str, component_name: str) -> None:
    """Remove a component through the components modal (process_project)."""
    modal = _open_components_modal(page, base_url, project_name)
    index = _component_index_in_modal(page, component_name)
    modal.sequence_remove("components", index)
    modal.submit_step_expect_progress()
    _await_progress(modal, project_name)
    modal.close_modal()


# ---------------------------------------------------------------------------
# Verification against the committed project file
# ---------------------------------------------------------------------------


def _component(data: dict, name: str) -> dict | None:
    for comp in data.get("components") or []:
        if isinstance(comp, dict) and comp.get("name") == name:
            return comp
    return None


def _deployment(data: dict, name: str) -> dict | None:
    for deployment in data.get("deployments") or []:
        if isinstance(deployment, dict) and deployment.get("name") == name:
            return deployment
    return None


def _deployment_component(data: dict, reference: str) -> dict | None:
    for deployment in data.get("deployments") or []:
        for comp in deployment.get("components") or []:
            if isinstance(comp, dict) and comp.get("reference") == reference:
                return comp
    return None


def _memory_limit(data: dict, component: str) -> str | None:
    resources = (_component(data, component) or {}).get("resources") or {}
    return (resources.get("limits") or {}).get("memory")


def _emails(data: dict) -> list[str]:
    return [u.get("email") for u in data.get("users") or []]


def _assert_in_git(
    forgejo: ForgejoClient,
    project_name: str,
    what: str,
    predicate: Callable[[dict], bool],
) -> dict:
    """Wait until the committed project file satisfies the predicate, else fail loudly.

    The full file goes into the message: with concurrent writes, what the file
    DOES contain is the diagnosis.
    """
    data = forgejo.wait_for_condition(project_name, predicate, timeout=GIT_TIMEOUT)
    assert data is not None, (
        f"Project file '{project_name}' in Forgejo never showed: {what}.\n"
        f"Current file:\n{forgejo.get_project_file(project_name)}"
    )
    return data


def _assert_env_vars_encrypted(forgejo: ForgejoClient, project_name: str, component_name: str, marker: str) -> None:
    """Env vars must be AGE-encrypted in git, with no plaintext value leaking."""
    data = _assert_in_git(
        forgejo,
        project_name,
        f"user-env-vars on component '{component_name}'",
        lambda d: isinstance((_component(d, component_name) or {}).get("user-env-vars"), str),
    )
    component = _component(data, component_name)
    assert component is not None
    assert AGE_ARMOR in component["user-env-vars"], (
        f"user-env-vars of '{component_name}' in '{project_name}' is not AGE-encrypted"
    )

    raw = forgejo.get_project_file(project_name) or ""
    key, _, value = marker.partition("=")
    assert value not in raw, f"Plaintext value of {key} leaked into the git project file of '{project_name}'"


# ---------------------------------------------------------------------------
# Rounds. Each fires concurrent changes at ONE file, for all five projects.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(1800)
def test_create_five_projects(reallife_projects: list[CreatedProject], forgejo: ForgejoClient) -> None:
    """All five wizard-created projects have a correct project file in Forgejo."""
    assert len(reallife_projects) == PROJECT_COUNT
    for project in reallife_projects:
        data = forgejo.get_project_yaml(project.name)
        assert data is not None, f"Project file for '{project.name}' missing in Forgejo"
        assert data.get("display-name") == project.display_name
        assert SANDBOX_TEST_USER["email"] in _emails(data)
        assert forgejo.component_names(project.name) == [WEB]
        assert _deployment_component(data, WEB) is not None


@pytest.mark.timeout(1800)
def test_concurrent_component_adds_on_same_file(
    reallife_projects: list[CreatedProject],
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> None:
    """Two component-adds race on each single project file; both must survive.

    This is the canonical lost-update shape: both tasks read the same YAML and
    append to the same `components` list. If the second write is published over
    the first instead of merged, one component silently disappears.
    """
    tasks: list[tuple[CreatedProject, str]] = []
    for project in reallife_projects:
        tasks.append((project, _start_add_component(sandbox_url, project, ALPHA)))
        tasks.append((project, _start_add_component(sandbox_url, project, BETA, env_vars=API_ENV_MARKER)))

    for project in reallife_projects:
        data = _assert_in_git(
            forgejo,
            project.name,
            f"both '{ALPHA}' and '{BETA}' present",
            lambda d: _component(d, ALPHA) is not None and _component(d, BETA) is not None,
        )
        # The pre-existing component must not have been collateral damage.
        assert _component(data, WEB) is not None, f"'{WEB}' vanished from '{project.name}' during concurrent adds"
        # Both must also be wired into the deployment, not just declared.
        for name in (WEB, ALPHA, BETA):
            assert _deployment_component(data, name) is not None, (
                f"'{name}' missing from the deployment of '{project.name}'"
            )
        _assert_env_vars_encrypted(forgejo, project.name, BETA, API_ENV_MARKER)

    _settle_tasks(sandbox_url, tasks)


@pytest.mark.timeout(1800)
def test_concurrent_patches_on_same_file(
    reallife_projects: list[CreatedProject],
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> None:
    """Three patches race on each file, each touching a different component.

    Distinct memory values per component mean a lost update shows up as a stale
    value rather than a missing field.
    """
    tasks: list[tuple[CreatedProject, str]] = []
    for project in reallife_projects:
        tasks.append((project, _start_component_patch(sandbox_url, project, WEB, {"memory_limit": MEM_WEB})))
        tasks.append((project, _start_component_patch(sandbox_url, project, ALPHA, {"memory_limit": MEM_ALPHA})))
        tasks.append((project, _start_component_patch(sandbox_url, project, BETA, {"memory_limit": MEM_BETA})))

    for project in reallife_projects:
        _assert_in_git(
            forgejo,
            project.name,
            f"memory limits {MEM_WEB}/{MEM_ALPHA}/{MEM_BETA} on {WEB}/{ALPHA}/{BETA}",
            lambda d: (
                _memory_limit(d, WEB) == MEM_WEB
                and _memory_limit(d, ALPHA) == MEM_ALPHA
                and _memory_limit(d, BETA) == MEM_BETA
            ),
        )

    _settle_tasks(sandbox_url, tasks)


@pytest.mark.timeout(1800)
def test_ui_edits_while_api_task_runs_on_same_file(
    reallife_projects: list[CreatedProject],
    sandbox_url: str,
    forgejo: ForgejoClient,
    ui_page: Page,
) -> None:
    """A browser edit lands while an API component-add is in flight on the SAME file.

    The API task is started first and deliberately not awaited, so the UI save
    happens against a file the task is about to rewrite. Both the new component
    and both UI changes must be in the final file.
    """
    tasks = [(project, _start_add_component(sandbox_url, project, GAMMA)) for project in reallife_projects]

    # Drive the UI while those tasks are still running.
    for project in reallife_projects:
        _edit_description_via_ui(ui_page, sandbox_url, project.name, NEW_DESCRIPTION)
        _add_team_member_via_ui(ui_page, sandbox_url, project.name, EXTRA_TEAM_EMAIL)

    for project in reallife_projects:
        data = _assert_in_git(
            forgejo,
            project.name,
            f"'{GAMMA}' plus the UI description and team change",
            lambda d: (
                _component(d, GAMMA) is not None
                and d.get("description") == NEW_DESCRIPTION
                and EXTRA_TEAM_EMAIL in _emails(d)
            ),
        )
        # Earlier rounds must still be intact underneath.
        assert SANDBOX_TEST_USER["email"] in _emails(data), f"original admin lost from '{project.name}'"
        assert _memory_limit(data, ALPHA) == MEM_ALPHA, f"earlier patch on '{ALPHA}' lost in '{project.name}'"

    _settle_tasks(sandbox_url, tasks)


@pytest.mark.timeout(1800)
def test_ui_env_vars_while_api_patches_same_file(
    reallife_projects: list[CreatedProject],
    sandbox_url: str,
    forgejo: ForgejoClient,
    ui_page: Page,
) -> None:
    """UI env-var edit on `web` races an API image update on the same file.

    Also proves the env-var round trip: encrypted in git, readable again in the UI.
    """
    tasks = [(p, _start_image_update(sandbox_url, p, WEB, ALT_IMAGE)) for p in reallife_projects]

    for project in reallife_projects:
        _set_env_vars_via_ui(ui_page, sandbox_url, project.name, WEB, UI_ENV_MARKER)

    for project in reallife_projects:
        _assert_in_git(
            forgejo,
            project.name,
            f"env vars on '{WEB}' and image {ALT_IMAGE}",
            lambda d: (
                isinstance((_component(d, WEB) or {}).get("user-env-vars"), str)
                and (_deployment_component(d, WEB) or {}).get("image") == ALT_IMAGE
            ),
        )
        _assert_env_vars_encrypted(forgejo, project.name, WEB, UI_ENV_MARKER)

        # De namen van de variabelen staan op het tabblad Deployments
        # (bg/_env-vars.html.j2 hangt onder active_tab == 'deployments'), niet op
        # Overzicht. En niet via text_content("body"): dat leest de lichte boom en de
        # naam staat in een <c-code> binnen een LOTC-component. De tekstselector van
        # Playwright kijkt wel door schaduwbomen heen.
        ui_page.goto(f"{sandbox_url}/projects/{project.name}/deployments")
        ui_page.wait_for_load_state("networkidle")
        key = UI_ENV_MARKER.partition("=")[0]
        assert ui_page.locator(f"text={key}").count() > 0, (
            f"Env var '{key}' not shown decrypted on the deployments tab of '{project.name}'"
        )

    _settle_tasks(sandbox_url, tasks)


@pytest.mark.timeout(1800)
@pytest.mark.xfail(
    reason=(
        "Productfout, buiten deze PR: een component uit de componenten-modal halen laat zijn "
        "verwijzing in de deployment staan, waarna het opslaan afketst op 'Invalid component "
        "references in deployment'. De API-route (project_manager.delete_component) haalt die "
        "verwijzingen wel weg; de modal doet dat niet, dus via de UI is een component dat in een "
        "deployment zit niet te verwijderen. Strict, zodat deze test gaat piepen zodra dat klopt."
    ),
    strict=True,
)
def test_ui_removal_while_api_patches_same_file(
    reallife_projects: list[CreatedProject],
    sandbox_url: str,
    forgejo: ForgejoClient,
    ui_page: Page,
) -> None:
    """The UI removes a component while an API patch rewrites another one, same file.

    A removal racing a write is the sharpest case: a text merge can easily
    resurrect the removed entry or drop the concurrent patch.
    """
    tasks = [(p, _start_component_patch(sandbox_url, p, ALPHA, {"path": "/alpha"})) for p in reallife_projects]

    for project in reallife_projects:
        _remove_component_via_ui(ui_page, sandbox_url, project.name, BETA)

    for project in reallife_projects:
        data = _assert_in_git(
            forgejo,
            project.name,
            f"'{BETA}' removed while the patch on '{ALPHA}' survived",
            lambda d: (
                _component(d, BETA) is None
                and _deployment_component(d, BETA) is None
                and _component(d, ALPHA) is not None
            ),
        )
        assert _memory_limit(data, ALPHA) == MEM_ALPHA, f"'{ALPHA}' patch lost during removal in '{project.name}'"
        for name in (WEB, GAMMA):
            assert _component(data, name) is not None, f"'{name}' lost during removal round in '{project.name}'"

    _settle_tasks(sandbox_url, tasks)


@pytest.mark.timeout(1800)
def test_add_deployment_rolls_out_whole_project(
    reallife_projects: list[CreatedProject],
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> None:
    """Adding a second deployment redeploys the entire project.

    This is the heaviest user action: a new deployment means new namespace-scoped
    resources and a fresh rollout of every component in it. The project file must
    end up with both deployments, each carrying the full component set, and the
    original deployment must be untouched.
    """
    components = [WEB, ALPHA, GAMMA]
    tasks = [(p, _start_add_deployment(sandbox_url, p, SECOND_DEPLOYMENT, components)) for p in reallife_projects]

    for project in reallife_projects:
        data = _assert_in_git(
            forgejo,
            project.name,
            f"deployment '{SECOND_DEPLOYMENT}' with {components}",
            lambda d: (
                _deployment(d, SECOND_DEPLOYMENT) is not None
                and {c.get("reference") for c in (_deployment(d, SECOND_DEPLOYMENT) or {}).get("components") or []}
                >= set(components)
            ),
        )
        # The deployment the wizard created must still be there, with its own components.
        original = _deployment(data, project.deployment_name)
        assert original is not None, f"original deployment '{project.deployment_name}' lost in '{project.name}'"
        original_refs = {c.get("reference") for c in original.get("components") or []}
        assert WEB in original_refs, f"'{WEB}' lost from the original deployment of '{project.name}'"

    _settle_tasks(sandbox_url, tasks)


@pytest.mark.timeout(900)
def test_final_state_of_all_projects(
    reallife_projects: list[CreatedProject],
    forgejo: ForgejoClient,
) -> None:
    """Cumulative end state of all five files: nothing was silently dropped.

    Each individual round only checks its own change. This checks every round at
    once, which is where a lost update from an earlier round becomes visible.
    """
    # BETA hoort hier: de verwijderronde erboven staat als xfail omdat de UI een
    # component dat in een deployment zit niet kan verwijderen, dus hij staat er nog.
    # Verdwijnt die xfail, dan hoort BETA hier ook weg.
    expected_components = sorted([WEB, ALPHA, BETA, GAMMA])
    problems: list[str] = []

    for project in reallife_projects:
        data = forgejo.get_project_yaml(project.name)
        if data is None:
            problems.append(f"{project.name}: project file missing")
            continue

        actual = sorted(forgejo.component_names(project.name))
        if actual != expected_components:
            problems.append(f"{project.name}: components {actual}, expected {expected_components}")
        if data.get("description") != NEW_DESCRIPTION:
            problems.append(f"{project.name}: description is {data.get('description')!r}")
        if _memory_limit(data, WEB) != MEM_WEB:
            problems.append(f"{project.name}: {WEB} memory {_memory_limit(data, WEB)}, expected {MEM_WEB}")
        if _memory_limit(data, ALPHA) != MEM_ALPHA:
            problems.append(f"{project.name}: {ALPHA} memory {_memory_limit(data, ALPHA)}, expected {MEM_ALPHA}")
        if (_deployment_component(data, WEB) or {}).get("image") != ALT_IMAGE:
            problems.append(f"{project.name}: {WEB} image is not {ALT_IMAGE}")
        if not isinstance((_component(data, WEB) or {}).get("user-env-vars"), str):
            problems.append(f"{project.name}: {WEB} has no encrypted user-env-vars")
        problems.extend(
            f"{project.name}: user {email} missing"
            for email in (SANDBOX_TEST_USER["email"], EXTRA_TEAM_EMAIL)
            if email not in _emails(data)
        )

    assert not problems, "Final project files are not consistent:\n" + "\n".join(problems)
