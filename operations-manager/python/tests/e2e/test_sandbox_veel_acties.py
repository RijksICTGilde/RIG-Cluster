"""RC-117 measurement: many actions on one project, on a live sandbox.

The research question is where the time actually goes when an agent fires ten
actions at one project in quick succession, and whether the commit-and-push per
action is the thing worth changing. Guessing at that from the code is how you end
up optimising the wrong step, so this drives the real API against a real cluster
and times each part.

Two phases, both on one project:

* **deferred** -- ten component-adds with ``rollout=false`` (project file only),
  then one ``:refresh`` that rolls all ten out at once;
* **rolled out** -- component-adds with ``rollout=true``, the default an agent gets
  today, each one paying for manifest generation and the ArgoCD wait.

Fewer rolled-out actions than deferred ones on purpose: each one costs minutes on
the shared Kind sandbox, and the number that matters is the per-action cost, not
the count. The report quotes the mean and says how many samples it rests on.

The timings are written to the artifact directory as JSON, so the numbers in
``docs/rc117-veel-acties-meting.md`` can be traced back to a run rather than to a
memory of one. The assertions are about structure, not duration: a wall-clock
threshold on a shared cluster is a flaky test, while "ten deferred changes are
reported as ten pending changes and none of them reached the cluster" is the
behaviour the recommendation actually rests on.

Run with:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=<the cluster's SECRET_KEY> \
    uv run pytest tests/e2e/test_sandbox_veel_acties.py -m "e2e and sandbox" -v --timeout=3600
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.lifecycle import RUNNABLE_IMAGE, CreatedProject, create_project_via_wizard
from tests.e2e.helpers.wizard import unique_project_name

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.slow]

_VERIFY_SSL = FORGEJO_VERIFY_SSL

#: Deferred actions in the batch. Ten is the number the research question names.
DEFERRED_ACTIONS = 10

#: Rolled-out actions. Each costs minutes on the shared sandbox; the per-action mean
#: is what the report quotes, so a handful of samples is what is needed.
ROLLED_OUT_ACTIONS = 3

#: A rolled-out component-add re-syncs the deployment and waits for ArgoCD, which on the
#: busy Kind sandbox runs well past the helper's 180s default (issue #130).
_ROLLOUT_TIMEOUT = 600.0

#: A deferred action writes the project file and stops, so it has no cluster wait at all.
_DEFERRED_TIMEOUT = 120.0


def _add_component(
    base_url: str,
    project: CreatedProject,
    component_name: str,
    *,
    rollout: bool,
    timeout: float,
) -> dict[str, Any]:
    """POST a component-add and wait for the task, honouring the rollout flag.

    ``sandbox_api.add_component`` does not carry the flag, and the flag is the whole
    point of this measurement, so the request is built here rather than widening a
    helper that the other suites use unchanged.
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": project.api_key, "Content-Type": "application/json"}
    body = {
        "name": component_name,
        "image": RUNNABLE_IMAGE,
        "deployment_names": [project.deployment_name],
    }
    with httpx.Client(verify=_VERIFY_SSL, timeout=30.0) as client:
        response = client.post(
            f"{base}/api/v2/projects/{project.name}/components",
            params={"rollout": "true" if rollout else "false"},
            json=body,
            headers=headers,
        )
        assert response.status_code == 202, (
            f"Expected 202 from component-add (rollout={rollout}), got {response.status_code}: {response.text}"
        )
        location = response.headers.get("Location")
        task_id = location.rsplit("/", 1)[-1] if location else response.json().get("task_id")
        assert task_id, f"No task id returned from component-add: {response.text}"

    return sandbox_api.wait_for_task(base_url, task_id, project.api_key, verify_ssl=_VERIFY_SSL, timeout=timeout)


def _pending_rollout(base_url: str, project: CreatedProject) -> dict[str, Any]:
    base = base_url.rstrip("/")
    with httpx.Client(verify=_VERIFY_SSL, timeout=30.0) as client:
        response = client.get(
            f"{base}/api/v2/projects/{project.name}/pending-rollout",
            headers={"X-API-Key": project.api_key},
        )
    assert response.status_code == 200, f"pending-rollout returned {response.status_code}: {response.text}"
    return response.json()


def _refresh(base_url: str, project: CreatedProject, timeout: float) -> dict[str, Any]:
    base = base_url.rstrip("/")
    headers = {"X-API-Key": project.api_key, "Content-Type": "application/json"}
    with httpx.Client(verify=_VERIFY_SSL, timeout=30.0) as client:
        response = client.post(f"{base}/api/v2/projects/{project.name}/:refresh", headers=headers)
        assert response.status_code == 202, f"Expected 202 from :refresh, got {response.status_code}: {response.text}"
        location = response.headers.get("Location")
        task_id = location.rsplit("/", 1)[-1] if location else response.json().get("task_id")
        assert task_id, f"No task id returned from :refresh: {response.text}"

    return sandbox_api.wait_for_task(base_url, task_id, project.api_key, verify_ssl=_VERIFY_SSL, timeout=timeout)


@pytest.fixture(scope="module")
def meting_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    """One project for the whole measurement, torn down afterwards."""
    page = sandbox_context.new_page()
    created: CreatedProject | None = None
    try:
        created = create_project_via_wizard(
            page,
            sandbox_url,
            forgejo,
            unique_project_name(),
            user_email=SANDBOX_TEST_USER["email"],
        )
        yield created
    finally:
        page.close()
        if created is not None:
            sandbox_api.delete_project_via_api(sandbox_url, created.name, created.api_key, verify_ssl=_VERIFY_SSL)


def test_veel_acties_op_een_project(
    meting_project: CreatedProject,
    sandbox_url: str,
    forgejo: ForgejoClient,
    artifact_dir: Path,
) -> None:
    """Measure ten deferred actions plus one rollout against rolled-out actions.

    Asserted (structure, not duration):

    1. every deferred action lands in the project file in Forgejo -- so "cheap" is not
       "did less";
    2. the ten deferred changes are reported as pending, which is what makes batching
       safe to recommend: the drift is visible rather than silent;
    3. one refresh clears all ten at once, so batching does not mean replaying them.
    """
    project = meting_project
    metingen: dict[str, Any] = {"project": project.name, "deferred": [], "rolled_out": [], "refresh": None}

    # -- phase 1: ten actions that write the project file and stop --------------
    for i in range(DEFERRED_ACTIONS):
        component = f"uitgesteld{i}"
        started = time.monotonic()
        task = _add_component(sandbox_url, project, component, rollout=False, timeout=_DEFERRED_TIMEOUT)
        elapsed = time.monotonic() - started
        metingen["deferred"].append({"component": component, "seconds": round(elapsed, 3)})
        logger.info("deferred action %d/%d (%s): %.2fs", i + 1, DEFERRED_ACTIONS, component, elapsed)

        processing = (task.get("result") or {}).get("processing") or {}
        assert processing.get("status") == "skipped", (
            f"rollout=false should have skipped processing for '{component}', got {processing!r}"
        )

    componenten = forgejo.component_names(project.name)
    ontbrekend = [f"uitgesteld{i}" for i in range(DEFERRED_ACTIONS) if f"uitgesteld{i}" not in componenten]
    assert not ontbrekend, f"deferred components missing from the project file in Forgejo: {ontbrekend}"

    pending = _pending_rollout(sandbox_url, project)
    assert pending["count"] == DEFERRED_ACTIONS, (
        f"expected {DEFERRED_ACTIONS} pending changes after {DEFERRED_ACTIONS} deferred actions, got {pending}"
    )

    # -- phase 2: one rollout for all ten ---------------------------------------
    started = time.monotonic()
    _refresh(sandbox_url, project, timeout=_ROLLOUT_TIMEOUT)
    refresh_seconds = time.monotonic() - started
    metingen["refresh"] = {"seconds": round(refresh_seconds, 3)}
    logger.info("one :refresh for %d deferred changes: %.2fs", DEFERRED_ACTIONS, refresh_seconds)

    na_refresh = _pending_rollout(sandbox_url, project)
    assert na_refresh["count"] == 0, f"one refresh should clear all deferred changes, got {na_refresh}"

    # -- phase 3: the same action as an agent gets it today ---------------------
    for i in range(ROLLED_OUT_ACTIONS):
        component = f"uitgerold{i}"
        started = time.monotonic()
        _add_component(sandbox_url, project, component, rollout=True, timeout=_ROLLOUT_TIMEOUT)
        elapsed = time.monotonic() - started
        metingen["rolled_out"].append({"component": component, "seconds": round(elapsed, 3)})
        logger.info("rolled-out action %d/%d (%s): %.2fs", i + 1, ROLLED_OUT_ACTIONS, component, elapsed)

    deferred_seconds = [m["seconds"] for m in metingen["deferred"]]
    rolled_out_seconds = [m["seconds"] for m in metingen["rolled_out"]]
    metingen["samenvatting"] = {
        "deferred_total": round(sum(deferred_seconds), 3),
        "deferred_mean": round(sum(deferred_seconds) / len(deferred_seconds), 3),
        "refresh_seconds": round(refresh_seconds, 3),
        "batch_total": round(sum(deferred_seconds) + refresh_seconds, 3),
        "rolled_out_mean": round(sum(rolled_out_seconds) / len(rolled_out_seconds), 3),
        "rolled_out_projected_10": round(sum(rolled_out_seconds) / len(rolled_out_seconds) * DEFERRED_ACTIONS, 3),
    }
    (artifact_dir / "rc117-veel-acties.json").write_text(json.dumps(metingen, indent=2))
    logger.info("RC-117 metingen: %s", json.dumps(metingen["samenvatting"], indent=2))
