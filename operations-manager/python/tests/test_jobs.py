"""Unit tests for the ad-hoc job run feature."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import yaml
from opi.core.templates import get_templates
from opi.generation.manifests import render_template
from opi.manager.job_manager import ANNOT_COMMAND, ANNOT_IMAGE, JobManager, JobRun
from opi.manager.run_support import ANNOT_EXPIRES, ANNOT_OPENED_BY, LABEL_RUN, LABEL_RUN_DEPLOYMENT
from opi.utils import naming


def test_resolve_image_local_and_remote():
    from opi.manager.run_support import resolve_image

    # local/ -> strip prefix + never pull (Kind-loaded image)
    assert resolve_image("local/job-test:latest") == ("job-test:latest", "Never")
    # remote image -> unchanged + default pull policy
    assert resolve_image("ghcr.io/org/app:1") == ("ghcr.io/org/app:1", "IfNotPresent")
    assert resolve_image("ghcr.io/org/app:1", "Always") == ("ghcr.io/org/app:1", "Always")


def test_generate_job_name():
    name = naming.generate_job_name("My-Proj", "Prod", "ab12cd34")
    assert name == "job-my-proj-prod-ab12cd34"
    assert len(name) <= 63


def _render_tasks(items) -> str:
    req = SimpleNamespace(scope={"type": "http"}, headers={}, state=SimpleNamespace())
    tmpl = get_templates().get_template("project-details/section-tasks.html.j2")
    return tmpl.render(request=req, project_name="proj", items=items)


def test_tasks_tab_empty():
    html = _render_tasks([])
    assert "nog geen taken" in html.lower()


def test_tasks_tab_lists_runs_and_tasks():
    # Unified rows as produced by router_tasks._normalize_run / _normalize_task.
    items = [
        {
            "soort": "Project verversen",
            "deployment": None,
            "status": "Bezig",
            "active": True,
            "step": "Deployments aanmaken",
            "progress": 45,
            "door": "u@x.nl",
            "gestart": "2026-06-28T10:00:00+00:00",
            "beeindigd": None,
        },
        {
            "soort": "Job",
            "deployment": "dep",
            "status": "Voltooid",
            "active": False,
            "step": None,
            "progress": None,
            "door": "u@x.nl",
            "gestart": "2026-06-28T09:00:00+00:00",
            "beeindigd": "2026-06-28T09:01:00+00:00",
        },
    ]
    html = _render_tasks(items)
    assert "Job" in html
    assert "Project verversen" in html  # background task from the other table
    assert "Voltooid" in html
    assert "Bezig" in html
    # A running task shows its live step + progress.
    assert "Deployments aanmaken" in html
    assert "45%" in html
    assert "2026-06-28 09:00" in html  # T replaced, truncated to minutes


def test_normalize_run_and_task():
    from opi.web.router_tasks import _normalize_run, _normalize_task

    run = _normalize_run({"kind": "db-console", "deployment": "dep", "status": "running", "started_by": "a@b.nl"})
    assert run["soort"] == "Databaseconsole"
    assert run["door"] == "a@b.nl"
    assert run["active"] is True
    assert run["status"] == "Bezig"  # status shown in Dutch
    assert run["step"] is None  # runs have no sub-step

    task = _normalize_task(
        {
            "task_type": "refresh_project",
            "status": "running",
            "created_by": "a@b.nl",
            "current_step": "Manifests genereren",
            "progress_percent": 30,
        }
    )
    assert task["soort"] == "Project verversen"
    assert task["active"] is True
    assert task["status"] == "Bezig"
    assert task["step"] == "Manifests genereren"
    assert task["progress"] == 30

    # A completed task is not active and reads as Voltooid.
    done = _normalize_task({"task_type": "refresh_project", "status": "completed"})
    assert done["active"] is False
    assert done["status"] == "Voltooid"
    # Unmapped types fall back to a humanized label.
    assert _normalize_task({"task_type": "some_new_type"})["soort"] == "Some new type"


def test_job_pod_renders_valid_yaml():
    doc = yaml.safe_load(
        render_template(
            "job-pod.yaml.jinja",
            {
                "name": "job-proj-dep-ab12",
                "namespace": "rig-proj",
                "project": {"name": "proj"},
                "cluster": "odcn-production",
                "extra_labels": {"rig.zad/run": "ab12", "rig.zad/run-kind": "job"},
                "extra_annotations": {"rig.zad/job-image": "img"},
                "target_deployment": "dep",
                "ttl_seconds": 3600,
                "image": "ghcr.io/x:1",
                "command": "alembic upgrade head",
                "db_secret_name": "dep-database",
            },
        )
    )
    assert doc["kind"] == "Pod"
    assert doc["spec"]["restartPolicy"] == "Never"
    assert doc["spec"]["activeDeadlineSeconds"] == 3600
    # deployment label = NetworkPolicy egress; app label = log stream target.
    assert doc["metadata"]["labels"]["deployment"] == "dep"
    assert doc["metadata"]["labels"]["app"] == "job-proj-dep-ab12"
    container = doc["spec"]["containers"][0]
    assert container["command"] == ["/bin/sh", "-c", "alembic upgrade head"]
    assert container["envFrom"] == [{"secretRef": {"name": "dep-database"}}]


def test_job_pod_without_command_runs_image_default():
    doc = yaml.safe_load(
        render_template(
            "job-pod.yaml.jinja",
            {
                "name": "job-proj-dep-ab12",
                "namespace": "rig-proj",
                "project": {"name": "proj"},
                "cluster": "local",
                "extra_labels": {},
                "extra_annotations": {},
                "target_deployment": "dep",
                "ttl_seconds": 3600,
                "image": "job-test",
                "command": "",
                "db_secret_name": None,
            },
        )
    )
    # No command override -> the image's own entrypoint/cmd runs.
    assert "command" not in doc["spec"]["containers"][0]


def test_job_pod_without_db_has_no_envfrom():
    doc = yaml.safe_load(
        render_template(
            "job-pod.yaml.jinja",
            {
                "name": "job-proj-dep-ab12",
                "namespace": "rig-proj",
                "project": {"name": "proj"},
                "cluster": "local",
                "extra_labels": {},
                "extra_annotations": {},
                "target_deployment": "dep",
                "ttl_seconds": 3600,
                "image": "busybox",
                "command": "echo hi",
                "db_secret_name": None,
            },
        )
    )
    assert "envFrom" not in doc["spec"]["containers"][0]


def _pod(phase: str) -> dict:
    return {
        "metadata": {
            "name": "job-proj-dep-ab12",
            "labels": {LABEL_RUN: "ab12", LABEL_RUN_DEPLOYMENT: "dep"},
            "annotations": {
                ANNOT_EXPIRES: "2026-06-27T22:00:00+00:00",
                ANNOT_OPENED_BY: "u@x.nl",
                ANNOT_IMAGE: "img:1",
                ANNOT_COMMAND: "alembic upgrade head",
            },
        },
        "status": {"phase": phase},
    }


def test_job_from_pod_maps_phase_to_state():
    assert JobManager._job_from_pod(_pod("Pending"), "rig-proj", "proj").state == "starting"
    assert JobManager._job_from_pod(_pod("Running"), "rig-proj", "proj").state == "running"
    assert JobManager._job_from_pod(_pod("Succeeded"), "rig-proj", "proj").state == "succeeded"
    assert JobManager._job_from_pod(_pod("Failed"), "rig-proj", "proj").state == "failed"
    run = JobManager._job_from_pod(_pod("Running"), "rig-proj", "proj")
    assert run.image == "img:1"
    assert run.command == "alembic upgrade head"


# ----------------------------------------------------- modal renders via ROOS


def _fake_request():
    return SimpleNamespace(state=SimpleNamespace(csrf_token="tok"), scope={"type": "http"}, headers={})


def _render_modal(**ctx) -> str:
    tmpl = get_templates().get_template("shared/_job-modal.html.j2")
    return tmpl.render(request=_fake_request(), **ctx)


def test_job_modal_form_renders():
    html = _render_modal(
        project_name="proj",
        deployment_name="dep",
        job=None,
        state="none",
        error=None,
        errors=None,
        form_image="",
        form_command="",
        ttl_seconds=3600,
        enabled=True,
    )
    assert "Job uitvoeren" in html
    assert 'name="image"' in html
    assert 'name="command"' in html


def test_job_modal_starting_without_job_shows_spinner_not_form():
    # Background provisioning: state=starting but the pod (job) isn't visible yet.
    # Must show the spinner + keep polling, NOT fall back to the form.
    html = _render_modal(
        project_name="proj",
        deployment_name="dep",
        job=None,
        state="starting",
        error=None,
        errors=None,
        form_image="",
        form_command="",
        ttl_seconds=3600,
        enabled=True,
    )
    assert "Job wordt gestart" in html
    assert "/projects/proj/jobs/dep/status" in html  # self-polls
    assert 'name="image"' not in html  # not the form


def test_job_modal_shows_field_error_inline():
    html = _render_modal(
        project_name="proj",
        deployment_name="dep",
        job=None,
        state="none",
        error=None,
        errors={"image": "Image is verplicht"},
        form_image="",
        form_command="",
        ttl_seconds=3600,
        enabled=True,
    )
    assert "Image is verplicht" in html  # inline field error, not an alert


def test_job_modal_running_renders_with_logs_and_stop():
    job = JobRun(
        session_id="ab12",
        name="job-proj-dep-ab12",
        namespace="rig-proj",
        project="proj",
        deployment="dep",
        image="img:1",
        command="alembic upgrade head",
        opened_by="u@x.nl",
        expires_at=datetime(2026, 6, 27, 22, 0, tzinfo=UTC),
        state="running",
    )
    html = _render_modal(
        project_name="proj", deployment_name="dep", job=job, state="running", error=None, ttl_seconds=3600, enabled=True
    )
    assert "Logs bekijken" in html
    assert "openLogViewer" in html
    assert "/projects/proj/jobs/ab12/stop" in html
    assert "/projects/proj/jobs/dep/status" in html  # self-polls while running


def test_job_modal_succeeded_renders():
    job = JobRun(
        session_id="ab12",
        name="job-proj-dep-ab12",
        namespace="rig-proj",
        project="proj",
        deployment="dep",
        image="img:1",
        command="echo hi",
        opened_by="u@x.nl",
        expires_at=datetime(2026, 6, 27, 22, 0, tzinfo=UTC),
        state="succeeded",
    )
    html = _render_modal(
        project_name="proj",
        deployment_name="dep",
        job=job,
        state="succeeded",
        error=None,
        ttl_seconds=3600,
        enabled=True,
    )
    assert "voltooid" in html.lower()
