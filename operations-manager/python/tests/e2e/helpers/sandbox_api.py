"""
Sandbox API helpers: read a project's API key from the UI and drive the
component-add REST endpoint.

The component-add endpoint (`POST /api/v2/projects/{name}/components`) is
authenticated with the project's own `X-API-Key`. That key is generated at
project creation and is only exposed decrypted on the project-details page, so
we scrape it from there, then use it for the async API call.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING

import httpx
from tests.e2e.helpers import cluster

if TYPE_CHECKING:
    from playwright.sync_api import Page


def read_api_key(page: Page, base_url: str, project_name: str) -> str:
    """Scrape the decrypted project API key from the project-details page.

    The details page renders the key in a LOTC secret-field: the element TEXT is a row
    of bullets and the plaintext sits in its `data-value` attribute, so the attribute is
    what we read. We scope to the stack carrying the "API Key" heading, otherwise the
    first match on the page is the project's AGE private key.
    """
    page.goto(f"{base_url.rstrip('/')}/projects/details/{project_name}")
    page.wait_for_load_state("networkidle")
    value = page.locator('.lotc-stack:has(h3:text-is("API Key")) .lotc-secret__value').first
    value.wait_for(state="attached", timeout=10000)
    api_key = (value.get_attribute("data-value") or "").strip()
    if not api_key:
        raise AssertionError(f"Could not read API key for project '{project_name}' from details page")
    return api_key


def start_task(
    base_url: str,
    method: str,
    path: str,
    api_key: str,
    body: dict,
    *,
    verify_ssl: bool = True,
) -> str:
    """Fire an async v1/v2 API request and return its task id WITHOUT waiting.

    Used by the real-life suite to start mutations on several projects at once;
    the server processes the tasks concurrently while the test continues (e.g.
    driving UI edits on other projects). Pair with wait_for_task().
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        response = client.request(method, f"{base}{path}", json=body, headers=headers)
        assert response.status_code == 202, (
            f"Expected 202 from {method} {path}, got {response.status_code}: {response.text}"
        )
        location = response.headers.get("Location")
        task_id = location.rsplit("/", 1)[-1] if location else response.json().get("task_id")
        assert task_id, f"No task id returned from {method} {path}: {response.text}"
        return task_id


def wait_for_task(
    base_url: str,
    task_id: str,
    api_key: str,
    *,
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> dict:
    """Poll a task started with start_task() until it completes successfully."""
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        return _wait_for_task(client, base, task_id, headers, timeout=timeout)


def task_outcome(
    base_url: str,
    task_id: str,
    api_key: str,
    *,
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> tuple[str, str | None]:
    """Poll a task and report its outcome without asserting.

    Returns ("completed", None), ("superseded", None), ("failed", reason) or
    ("running", None) when the task has not reached a terminal state before the
    timeout. "superseded" is a benign outcome: a newer task whose scope covers
    this one took over, so this task's ArgoCD wait was abandoned by design. It is
    recorded as a completed task carrying result.status == "superseded".
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    deadline = time.monotonic() + timeout
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        while time.monotonic() < deadline:
            response = client.get(f"{base}/api/tasks/{task_id}", headers=headers)
            if response.status_code == 200:
                task = response.json()
                status = task.get("status")
                if status != "completed":
                    return "failed", f"task status '{status}': {task}"
                if (task.get("result") or {}).get("status") == "superseded":
                    return "superseded", None
                try:
                    _assert_no_subtask_failure(task_id, task)
                except AssertionError as exc:
                    return "failed", str(exc)
                return "completed", None
            if response.status_code != 202:
                return "failed", f"unexpected task poll status {response.status_code}: {response.text}"
            time.sleep(3.0)
    return "running", None


def add_component(
    base_url: str,
    project_name: str,
    api_key: str,
    *,
    component_name: str,
    image: str,
    deployment_names: list[str],
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> dict:
    """Add one component via the v2 async API and wait for the task to finish.

    Returns the terminal task response dict. Raises AssertionError if the task
    does not reach 'completed'.
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    body = {
        "name": component_name,
        "image": image,
        "deployment_names": deployment_names,
    }
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        response = client.post(
            f"{base}/api/v2/projects/{project_name}/components",
            json=body,
            headers=headers,
        )
        assert response.status_code == 202, (
            f"Expected 202 from component-add, got {response.status_code}: {response.text}"
        )
        location = response.headers.get("Location")
        task_id = location.rsplit("/", 1)[-1] if location else response.json().get("task_id")
        assert task_id, f"No task id returned from component-add: {response.text}"

        return _wait_for_task(client, base, task_id, headers, timeout=timeout)


def delete_project_via_api(
    base_url: str,
    project_name: str,
    api_key: str,
    *,
    force: bool = True,
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> None:
    """Safety-net deletion via the v1 API (DELETE /api/projects/{name}).

    Best-effort: swallows connection errors so it never breaks fixture teardown.
    Waits for the async delete task to finish when a task id is returned.
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    body = {"confirmDeletion": True, "force": force}
    with contextlib.suppress(httpx.HTTPError), httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        response = client.request(
            "DELETE",
            f"{base}/api/projects/{project_name}",
            json=body,
            headers=headers,
        )
        if response.status_code == 202:
            location = response.headers.get("Location")
            task_id = location.rsplit("/", 1)[-1] if location else None
            if task_id:
                with contextlib.suppress(AssertionError):
                    _wait_for_task(client, base, task_id, headers, timeout=timeout)

    # Belt-and-suspenders: the project delete only drops the namespace once the
    # ArgoCD app deletion is confirmed, which frequently times out on a busy
    # sandbox and leaves an empty namespace + dangling app that pile up across
    # runs. Reclaim them directly so test projects never accumulate.
    cluster.force_cleanup_project(project_name)


def _wait_for_task(
    client: httpx.Client,
    base: str,
    task_id: str,
    headers: dict,
    *,
    timeout: float,
    interval: float = 3.0,
) -> dict:
    """Poll GET /api/tasks/{task_id} until terminal (200), then assert real success.

    The task envelope reports status 'completed' even when the underlying operation
    failed - the actual outcome is in `result.status` and in the subtasks. We inspect
    both so a failed operation surfaces its real error instead of a vague downstream
    assertion.
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"{base}/api/tasks/{task_id}", headers=headers)
        # 202 = still running, 200 = terminal (completed/failed/cancelled)
        if resp.status_code == 200:
            last = resp.json()
            status = last.get("status")
            assert status == "completed", f"Task {task_id} ended with status '{status}': {last}"
            _assert_no_subtask_failure(task_id, last)
            return last
        if resp.status_code not in (200, 202):
            raise AssertionError(f"Unexpected task poll status {resp.status_code}: {resp.text}")
        last = resp.json()
        time.sleep(interval)
    raise AssertionError(f"Task {task_id} did not complete within {timeout}s (last: {last})")


def _assert_no_subtask_failure(task_id: str, task: dict) -> None:
    """Raise with the real error if the task's result or any subtask failed."""
    result = task.get("result") or {}
    if result.get("status") == "failed":
        error = task.get("error_message") or result.get("error") or "unknown error"
        raise AssertionError(f"Task {task_id} operation failed: {error} (result={result})")
    for sub in task.get("subtasks") or []:
        if sub.get("status") == "failed":
            raise AssertionError(
                f"Task {task_id} subtask '{sub.get('name')}' failed: {sub.get('error') or 'unknown error'}"
            )
