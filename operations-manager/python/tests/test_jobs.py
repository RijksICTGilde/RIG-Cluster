"""Unit tests for the ad-hoc job run feature."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from opi.core.templates_lotc import templates_lotc
from opi.generation.manifests import render_template
from opi.manager.job_manager import ANNOT_COMMAND, ANNOT_IMAGE, JobError, JobManager, JobRun
from opi.manager.run_support import ANNOT_EXPIRES, ANNOT_OPENED_BY, LABEL_RUN, LABEL_RUN_DEPLOYMENT
from opi.services.catalog.image_registries.naming import organization_name
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
    tmpl = templates_lotc.get_template("bg/_tasks.html.j2")
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
    # Sinds RC-133 gaat de datum door het gedeelde datumfilter: leesbaar, compacte maand,
    # en in onze eigen tijdzone. 09:00 UTC is in juni 11:00 in Amsterdam.
    assert "28 jun 2026 11:00" in html


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
    tmpl = templates_lotc.get_template("shared/_job-modal.html.j2")
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


class TestDeJobImageIsGeenVrijeKeuze:
    """De tweede blokkerende vondst uit de securityreview, op de route die geen
    projectbestand kent.

    ``apply_bundle`` krijgt sinds deze branch ``project_data`` mee, dus de PROJECTregels
    gelden ook voor de door een gebruiker INGETYPTE job-image: een image onder andermans
    proxy-organisatie krijgt het pull-secret van dat project aangehangen en elk projectlid
    mag zo'n job starten. De validators bij het opslaan zien deze image nooit, want die lopen
    over ``deployments[].components[].image`` in het projectbestand.
    """

    ODCN = "odcn-production"

    def _manager(self, kubectl: Any) -> Any:
        with patch("opi.manager.job_manager.create_kubectl_connector", return_value=kubectl):
            return JobManager()

    def _project(self, name: str) -> Any:
        return SimpleNamespace(
            name=name,
            data={
                "name": name,
                "deployments": [{"name": "prod", "cluster": self.ODCN, "namespace": name, "components": []}],
            },
        )

    def _store(self, *project_names: str):
        projects = {name: self._project(name) for name in project_names}
        store = MagicMock()
        store.get.side_effect = projects.get
        store.get_all.return_value = list(projects.values())
        return patch("opi.manager.job_manager.get_project_store", return_value=store), patch(
            "opi.services.project_store.get_project_store", return_value=store
        )

    async def _begin(self, image: str) -> str | None:
        """De foutmelding van ``begin()``, of None als hij de image accepteert."""
        kubectl = MagicMock()
        kubectl.get_resources_by_label = AsyncMock(return_value=[])
        runs = MagicMock()
        runs.get_latest_run = AsyncMock(return_value=None)
        runs.create_run = AsyncMock(return_value=None)
        job_store, validation_store = self._store("eigen", "slachtoffer")
        with (
            job_store,
            validation_store,
            patch("opi.core.config.settings.CLUSTER_MANAGER", self.ODCN),
            patch("opi.manager.job_manager.get_runs_service", return_value=runs),
        ):
            manager = self._manager(kubectl)
            try:
                await manager.begin("eigen", "prod", image, "sh -c id", "u@x.nl")
            except JobError as error:
                return str(error)
        return None

    @pytest.mark.asyncio
    async def test_andermans_proxy_organisatie_wordt_geweigerd(self) -> None:
        organisatie = organization_name("ghcr.io/team", "rig", "slachtoffer")
        melding = await self._begin(f"rcr.rijksapps.nl/{organisatie}/geheime-app:1")
        assert melding is not None
        assert "slachtoffer" in melding

    @pytest.mark.asyncio
    async def test_de_eigen_proxy_organisatie_mag(self) -> None:
        """De tegenproef op de toegestane kant: zonder deze had de weigering ook op een
        te brede grendel kunnen slaan."""
        organisatie = organization_name("ghcr.io/team", "rig", "eigen")
        assert await self._begin(f"rcr.rijksapps.nl/{organisatie}/eigen-app:1") is None

    @pytest.mark.asyncio
    async def test_een_gedeelde_proxy_en_een_gewone_image_mogen(self) -> None:
        assert await self._begin("rcr.rijksapps.nl/ghcr-rig/library/alpine:3") is None
        assert await self._begin("ghcr.io/eigen/app:1") is None
