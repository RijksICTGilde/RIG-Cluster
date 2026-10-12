"""Een refresh is een resetknop, dus hij breekt ook een sync af die net begonnen is (RC-251).

RC-250 gaf de ronde voor elke push een drempel van 300s, en die is daar juist: ze loopt ook
bij een sweep, een OOM- of VPA-tuner en een member-add, en dan is "deze uitrol is nog gewoon
bezig" een echte mogelijkheid. Een nachtelijke sweep mag de uitrol niet afbreken die een
ontwikkelaar twintig seconden eerder startte.

Bij een refresh is dat geen mogelijkheid meer. Iemand drukt op reset, en een reset die soms
wel en soms niet reset is erger dan een uitrol van enkele seconden die opnieuw begint.

De vlag hangt daarom aan het ENDPOINT en niet aan de handler: ``handle_refresh_project`` wordt
ook intern aangeroepen uit het component-delete-pad, en dat hoort het gedrag niet stilzwijgend
te erven. Die aanroeper zet hem met opzet wel, en dat staat hier vast als keuze.

Wat verder gemeten wordt: de vlag moet de hele keten afleggen (endpoint, payload, handler,
``process_project_from_git``, ``process_project``, de manifestronde, de repositoryronde, de
opruimronde). Elke hop die hem laat vallen geeft zonder eigen toets nul rood, want de stand
eronder is de oude.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.task_models import ProcessingStatus
from opi.api.v2.router import v2_router
from opi.core.task_handlers_operations import handle_refresh_deployment, handle_refresh_project
from opi.manager.project_manager import ProjectManager
from opi.server import app
from opi.services.argocd_operation import STUCK_OPERATION_THRESHOLD_SECONDS
from opi.services.oom_watcher import _queue_refresh_task as queue_oom_refresh_task
from tests.test_verdwenen_applicatie_tijdens_wacht import _progress, _project_manager

PROJECT = "dd-mco"
DEPLOYMENT = "pr-68"
APP = "dd-mco-pr-68"
ARGO_NS = "rig-prd-operations"


def _application(age_seconds: int) -> dict[str, Any]:
    """Een Application met een lopende, nog AANGEHECHTE sync van ``age_seconds`` oud."""
    started = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat().replace("+00:00", "Z")
    return {
        "operation": {"sync": {"revision": "6a0295a671"}},
        "status": {
            "operationState": {
                "phase": "Running",
                "startedAt": started,
                "syncResult": {"revision": "6a0295a671"},
                "message": "waiting for healthy state of apps/Deployment/pr-68-website",
            }
        },
    }


def _manager(age_seconds: int) -> MagicMock:
    fake = MagicMock(spec=ProjectManager)
    fake.get_name = AsyncMock(return_value=PROJECT)
    fake._kubectl_connector = MagicMock()
    fake._kubectl_connector.get_argocd_application = AsyncMock(return_value=_application(age_seconds))
    fake._kubectl_connector.terminate_argocd_application_operation = AsyncMock(return_value=True)
    # Instance-attribuut, dus ``spec=ProjectManager`` levert hem niet.
    fake._terminated_sync_operations = []
    return fake


# Jonger dan de drempel van 300s, dus precies wat de ronde voor de push laat staan. 299 is de
# rand: op 300 breekt ook de gewone push af.
JONG = [0, 5, 60, 299]


class TestDeDrempelOpHetRefreshPad:
    @pytest.mark.parametrize("age_seconds", JONG)
    async def test_een_jonge_operation_wordt_bij_een_refresh_wel_beeindigd(self, age_seconds: int) -> None:
        manager = _manager(age_seconds)

        await ProjectManager._clear_blocking_sync_operations(
            manager, [{"name": DEPLOYMENT, "cluster": "odcn-production"}], True
        )

        manager._kubectl_connector.terminate_argocd_application_operation.assert_awaited_once_with(APP, ARGO_NS)

    @pytest.mark.parametrize("age_seconds", JONG)
    async def test_dezelfde_operation_blijft_bij_een_gewone_push_staan(self, age_seconds: int) -> None:
        """De tegenproef, en de reden dat de drempel niet gewoon op 0 gezet is: deze ronde loopt
        ook bij een sweep en bij een tuner, en daar is "nog gewoon bezig" een echte
        mogelijkheid."""
        manager = _manager(age_seconds)

        await ProjectManager._clear_blocking_sync_operations(
            manager, [{"name": DEPLOYMENT, "cluster": "odcn-production"}]
        )

        manager._kubectl_connector.terminate_argocd_application_operation.assert_not_awaited()

    async def test_een_werkelijk_vastgelopen_operation_breekt_ook_zonder_de_vlag_af(self) -> None:
        """Wat RC-250 bouwde blijft staan: op de drempel breekt de gewone push wel af."""
        manager = _manager(STUCK_OPERATION_THRESHOLD_SECONDS)

        await ProjectManager._clear_blocking_sync_operations(
            manager, [{"name": DEPLOYMENT, "cluster": "odcn-production"}]
        )

        manager._kubectl_connector.terminate_argocd_application_operation.assert_awaited_once()

    async def test_op_projectniveau_geldt_het_voor_elke_deployment(self) -> None:
        """De knop zit op het project, dus de grote hamer is de bedoeling."""
        manager = _manager(5)

        await ProjectManager._clear_blocking_sync_operations(
            manager,
            [
                {"name": DEPLOYMENT, "cluster": "odcn-production"},
                {"name": "productie", "cluster": "odcn-production"},
            ],
            True,
        )

        assert [
            call.args[0] for call in manager._kubectl_connector.terminate_argocd_application_operation.await_args_list
        ] == [APP, "dd-mco-productie"]


class TestDeVlagLooptDoorDeKeten:
    """Per hop, want een hop die de vlag laat vallen valt terug op de oude stand: nul rood."""

    @pytest.mark.parametrize("vlag", [True, False])
    async def test_de_repositoryronde_geeft_hem_door_aan_de_opruimronde(self, vlag: bool) -> None:
        git = MagicMock()
        git.get_working_dir = AsyncMock(return_value="/tmp/repo")
        git.commit_changes = AsyncMock()
        git.push_changes = AsyncMock()

        fake = MagicMock(spec=ProjectManager)
        fake.get_contents = AsyncMock(return_value={"name": PROJECT})
        fake.get_git_connector_for_deployment = AsyncMock(return_value=git)
        fake._process_deployment_manifests = AsyncMock()
        fake.get_deployments = AsyncMock(return_value=[])
        fake._clear_blocking_sync_operations = AsyncMock()

        deployments = [{"name": DEPLOYMENT, "cluster": "odcn-production", "namespace": PROJECT}]
        await ProjectManager._process_repository_manifests(fake, {"name": "main-repo"}, deployments, vlag)

        fake._clear_blocking_sync_operations.assert_awaited_once_with(deployments, reset_running_sync=vlag)

    @pytest.mark.parametrize("vlag", [True, False])
    async def test_de_manifestronde_geeft_hem_door_per_repository(self, vlag: bool) -> None:
        deployments = [{"name": DEPLOYMENT, "cluster": "odcn-production", "repository": "main-repo"}]
        fake = MagicMock(spec=ProjectManager)
        fake.get_name = AsyncMock(return_value=PROJECT)
        fake.get_repositories = AsyncMock(return_value=[{"name": "main-repo"}])
        fake.get_deployments = AsyncMock(return_value=deployments)
        fake._process_repository_manifests = AsyncMock()

        await ProjectManager._process_application_manifests(fake, reset_running_sync=vlag)

        assert fake._process_repository_manifests.await_args.kwargs["reset_running_sync"] is vlag

    @pytest.mark.parametrize("vlag", [True, False])
    async def test_process_project_geeft_hem_door_aan_de_manifestronde(
        self, monkeypatch: pytest.MonkeyPatch, vlag: bool
    ) -> None:
        pm = _echte_project_manager(monkeypatch)

        assert await pm.process_project(reset_running_sync=vlag) is True

        assert pm._process_application_manifests.await_args.kwargs["reset_running_sync"] is vlag

    @pytest.mark.parametrize("handler", [handle_refresh_project, handle_refresh_deployment])
    @pytest.mark.parametrize(("payload_vlag", "verwacht"), [({"reset_running_sync": True}, True), ({}, False)])
    async def test_de_handler_geeft_de_payloadvlag_door(
        self, handler: Any, payload_vlag: dict[str, bool], verwacht: bool
    ) -> None:
        """Hier landt wat het endpoint in de payload zette. Ontbreekt de sleutel, dan is dit een
        gewone verwerking: de interne aanroeper die niets zegt erft het gedrag niet."""
        manager = await _run_handler_manager(handler, payload_vlag)

        assert manager.process_project_from_git.await_args.kwargs["reset_running_sync"] is verwacht

    @pytest.mark.parametrize("vlag", [True, False])
    async def test_process_project_from_git_geeft_hem_door(self, monkeypatch: pytest.MonkeyPatch, vlag: bool) -> None:
        manager = _project_manager(monkeypatch, wacht_fout=TimeoutError())

        await manager.process_project_from_git(
            "projects/mpfb-8wh.yaml", task_progress_manager=_progress(), reset_running_sync=vlag
        )

        assert manager.process_project.await_args.kwargs["reset_running_sync"] is vlag

    async def test_een_gewone_push_blijft_de_standaard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Zonder de vlag mee te geven: de sweep, de tuners en de member-add komen hier ook
        langs, en die horen hun drempel te houden."""
        manager = _project_manager(monkeypatch, wacht_fout=TimeoutError())

        await manager.process_project_from_git("projects/mpfb-8wh.yaml", task_progress_manager=_progress())

        assert manager.process_project.await_args.kwargs["reset_running_sync"] is False


class TestWieDeVlagZet:
    """De vlag hoort bij het endpoint, want een interne aanroeper mag hem niet erven."""

    @pytest.fixture
    def client(self) -> TestClient:
        app = FastAPI()
        app.include_router(v2_router)
        return TestClient(app)

    @pytest.fixture
    def store(self):
        project = MagicMock()
        project.name = PROJECT
        project.api_key = "test-key"
        project.filename = f"{PROJECT}.yaml"
        project.data = {"name": PROJECT, "deployments": [{"name": DEPLOYMENT}]}
        instance = MagicMock()
        instance.get.return_value = project
        with (
            patch("opi.api.endpoint_util.get_project_store", return_value=instance),
            patch("opi.api.v2.router.get_project_store", return_value=instance),
        ):
            yield instance

    @pytest.fixture
    def created_task(self):
        with patch("opi.api.v2.router.create_async_task", new=AsyncMock(return_value={"task_id": "t-1"})) as mock:
            yield mock

    @pytest.mark.parametrize(
        "url",
        [
            f"/api/v2/projects/{PROJECT}/:refresh",
            f"/api/v2/projects/{PROJECT}/deployments/{DEPLOYMENT}/:refresh",
        ],
    )
    def test_beide_v2_refresh_endpoints_zetten_hem(self, client, store, created_task, url: str) -> None:
        assert client.post(url, headers={"X-API-Key": "test-key"}).status_code == 202

        assert created_task.await_args.kwargs["payload"]["reset_running_sync"] is True

    async def test_de_knop_op_de_projectpagina_zet_hem(self) -> None:
        """De knop "Herverwerken" post naar de web-route, niet naar v2 (``project_actions.py``
        bouwt ``/projects/{p}/refresh``). Zonder de vlag hier reset de resetknop de lopende
        sync dus niet, en dat is de plek waar iemand werkelijk op reset drukt."""
        from opi.web.router import refresh_project_web

        with (
            patch("opi.web.router.get_current_user", return_value={"email": "boss@example.com"}),
            patch("opi.web.router.is_user_authorized_for_project", return_value=True),
            patch("opi.web.router.get_user_role_for_project", return_value="admin"),
            patch("opi.web.router.get_project_store", return_value=_store_with(PROJECT, DEPLOYMENT)),
            patch("opi.web.router.create_task_and_render_progress", AsyncMock()) as start_task,
        ):
            await refresh_project_web(object(), PROJECT)  # type: ignore[arg-type]

        assert start_task.await_args.kwargs["payload"]["reset_running_sync"] is True

    async def test_de_knop_bij_een_deployment_zet_hem_ook(self) -> None:
        from opi.web.router import refresh_deployment_web

        with (
            patch("opi.web.router.get_current_user", return_value={"email": "boss@example.com"}),
            patch("opi.web.router.is_user_authorized_for_project", return_value=True),
            patch("opi.web.router.get_user_role_for_project", return_value="admin"),
            patch("opi.web.router.get_project_store", return_value=_store_with(PROJECT, DEPLOYMENT)),
            patch("opi.web.router.create_task_and_render_progress", AsyncMock()) as start_task,
        ):
            await refresh_deployment_web(object(), PROJECT, DEPLOYMENT)  # type: ignore[arg-type]

        assert start_task.await_args.kwargs["payload"]["reset_running_sync"] is True

    @pytest.mark.parametrize("aanroeper", ["oom_watcher", "project_manager"])
    async def test_een_automatische_refresh_laat_de_vlag_weg(self, aanroeper: str) -> None:
        """De tegenhanger van het delete-pad hieronder. Beide plekken zetten zelf een
        ``refresh_deployment`` in de wachtrij zonder dat iemand op reset drukte, en daar is
        "deze uitrol is nog gewoon bezig" een echte mogelijkheid. Zonder deze toets kan de vlag
        er stil bij glippen en reset een automatische hersteltaak een uitrol van iemand anders.
        """
        task_service = AsyncMock()

        if aanroeper == "oom_watcher":
            with patch("opi.services.oom_watcher._task_service_ref", task_service):
                await queue_oom_refresh_task(PROJECT, DEPLOYMENT)
        else:
            await ProjectManager._queue_refresh_task(MagicMock(spec=ProjectManager), task_service, PROJECT, DEPLOYMENT)

        payload = task_service.create_task.await_args.kwargs["payload"]
        assert "reset_running_sync" not in payload

    async def test_het_component_delete_pad_zet_hem_expliciet(self) -> None:
        """Met opzet AAN: een verwijdering moet landen, en een sync op de oude staat hoort dat
        niet te vertragen. Deze toets legt vast dat het een keuze was en geen erfenis: zou de
        handler het gedrag zelf aanzetten, dan stond hier niets om op te wijzen."""
        from opi.core.task_handlers_components import handle_delete_component

        progress = MagicMock()
        progress.add_task.return_value = "t"
        manager = MagicMock()
        manager.delete_component = AsyncMock(return_value={"success": True, "uncoupled_from": []})
        manager.close = AsyncMock()

        with (
            # De import is functie-lokaal, dus de patch hoort bij de definierende module.
            patch("opi.manager.project_manager.ProjectManager", return_value=manager),
            patch(
                "opi.core.task_handlers_operations.handle_refresh_project",
                new=AsyncMock(return_value={"status": "completed"}),
            ) as refresh,
        ):
            await handle_delete_component(
                {"project_name": PROJECT, "component_name": "web", "confirm_in_use": False}, progress
            )

        assert refresh.await_args.args[0]["reset_running_sync"] is True


def _store_with(project_name: str, deployment_name: str) -> MagicMock:
    project = MagicMock()
    project.data = {"name": project_name, "deployments": [{"name": deployment_name}]}
    store = MagicMock()
    store.get.return_value = project
    return store


class TestDeTaakuitkomst:
    """Breekt een refresh een uitrol van iemand anders af, dan hoort dat hardop gezegd.

    Anders ziet de ontwikkelaar van wie de uitrol werd afgebroken een mislukking zonder
    oorzaak.
    """

    async def test_de_opruimronde_noemt_de_afgebroken_sync_in_de_voortgang(self) -> None:
        manager = _manager(5)
        manager._begin_step = MagicMock(return_value="stap-1")
        manager._end_step = MagicMock()

        await ProjectManager._clear_blocking_sync_operations(
            manager, [{"name": DEPLOYMENT, "cluster": "odcn-production"}], True
        )

        assert manager._begin_step.call_args.args[0] == f"Lopende sync afgebroken: {DEPLOYMENT}"
        manager._end_step.assert_called_once_with("stap-1")

    async def test_een_gewone_push_meldt_een_vastgelopen_sync_ook(self) -> None:
        """De melding hangt aan "er is werkelijk iets afgebroken", niet aan de vlag: ook de
        ronde met drempel 300 zegt hardop wat zij opruimde."""
        manager = _manager(STUCK_OPERATION_THRESHOLD_SECONDS)
        manager._begin_step = MagicMock(return_value="stap-1")
        manager._end_step = MagicMock()

        await ProjectManager._clear_blocking_sync_operations(
            manager, [{"name": DEPLOYMENT, "cluster": "odcn-production"}]
        )

        assert manager._begin_step.call_args.args[0] == f"Lopende sync afgebroken: {DEPLOYMENT}"
        assert manager._terminated_sync_operations == [DEPLOYMENT]

    async def test_zonder_afgebroken_sync_komt_er_geen_stap(self) -> None:
        """Een stap die er staat hoort bij werk dat liep. Niets afgebroken, niets te melden."""
        manager = _manager(5)
        manager._begin_step = MagicMock(return_value="stap-1")

        await ProjectManager._clear_blocking_sync_operations(
            manager, [{"name": DEPLOYMENT, "cluster": "odcn-production"}]
        )

        manager._begin_step.assert_not_called()
        assert manager._terminated_sync_operations == []

    async def test_de_opruimronde_onthoudt_wat_ze_afbrak(self) -> None:
        manager = _manager(5)

        await ProjectManager._clear_blocking_sync_operations(
            manager,
            [{"name": DEPLOYMENT, "cluster": "odcn-production"}, {"name": "productie", "cluster": "odcn-production"}],
            True,
        )

        assert manager._terminated_sync_operations == [DEPLOYMENT, "productie"]

    async def test_elke_run_begint_met_een_lege_lijst(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """De lijst groeit binnen een run mee over alle repositories, dus hij moet aan het begin
        van een run leeg. Anders meldt een tweede verwerking op dezelfde manager de afgebroken
        sync van de eerste nog een keer, bij een taak die niets afbrak."""
        pm = _echte_project_manager(monkeypatch)
        pm._terminated_sync_operations = ["van-de-vorige-run"]

        assert await pm.process_project() is True

        assert pm.get_terminated_sync_operations() == []

    async def test_een_mislukte_patch_telt_niet_als_afgebroken(self) -> None:
        """``terminate_stuck_operation`` geeft False als de patch niet lukte of kubectl weg was.
        Dan is er niets afgebroken, en dan mag de uitkomst dat ook niet zeggen."""
        manager = _manager(5)
        manager._kubectl_connector.terminate_argocd_application_operation = AsyncMock(return_value=False)

        await ProjectManager._clear_blocking_sync_operations(
            manager, [{"name": DEPLOYMENT, "cluster": "odcn-production"}], True
        )

        assert manager._terminated_sync_operations == []

    @pytest.mark.parametrize("handler", [handle_refresh_project, handle_refresh_deployment])
    @pytest.mark.parametrize("geslaagd", [True, False])
    async def test_de_uitkomst_draagt_de_afgebroken_syncs(self, handler: Any, geslaagd: bool) -> None:
        """Ook op de mislukte tak: wie zijn uitrol kwijt is leest juist daar."""
        result = await _run_handler(handler, terminated=[DEPLOYMENT], processing_result=geslaagd)

        assert result["processing"]["terminated_syncs"] == [DEPLOYMENT]

    @pytest.mark.parametrize("handler", [handle_refresh_project, handle_refresh_deployment])
    async def test_zonder_afgebroken_sync_staat_het_veld_er_niet(self, handler: Any) -> None:
        result = await _run_handler(handler, terminated=[], processing_result=True)

        assert "terminated_syncs" not in result["processing"]

    def test_het_antwoordmodel_draagt_het_veld(self) -> None:
        """Zonder dit veld op ``ProcessingStatus`` laat pydantic het stil uit het antwoord
        vallen, en dan staat het in de handler maar niet bij de client."""
        status = ProcessingStatus(status="completed", terminated_syncs=[DEPLOYMENT])

        assert status.model_dump()["terminated_syncs"] == [DEPLOYMENT]

    @pytest.mark.parametrize(
        "pad",
        [
            "/api/projects/{project_name}/:refresh",
            "/api/projects/{project_name}/deployments/{deployment_name}/:refresh",
        ],
    )
    def test_de_spec_van_de_inline_refreshes_noemt_het_veld(self, pad: str) -> None:
        """Er zijn twee ``ProcessingStatus``-modellen; de inline refreshes gebruiken die uit
        ``opi.api.router``, dus zonder het veld daar kent de OpenAPI het niet."""
        spec = app.openapi()
        schemas = spec["components"]["schemas"]
        antwoord = schemas[
            spec["paths"][pad]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].rsplit(
                "/", 1
            )[-1]
        ]
        processing = schemas[antwoord["properties"]["processing"]["$ref"].rsplit("/", 1)[-1]]

        assert "terminated_syncs" in processing["properties"]


def _echte_project_manager(monkeypatch: pytest.MonkeyPatch) -> ProjectManager:
    """De echte ``process_project``, met alleen zijn cluster- en git-stappen gemockt.

    ``tests/test_project_reconciliation.py`` heeft een vrijwel gelijke ``_manager()``, maar die
    schrijft met opzet ECHT naar Postgres (fixture ``orm_db``). Hier is de reconciliatie niet het
    onderwerp, dus die gaat hier naar een mock in plaats van dat deze toets een database nodig
    heeft.
    """
    monkeypatch.setattr("opi.manager.project_manager.provisioning_services", list)
    pm = ProjectManager(project_file_relative_path=f"projects/{PROJECT}.yaml")
    pm.get_contents = AsyncMock(return_value={"name": PROJECT, "deployments": []})
    pm.get_name = AsyncMock(return_value=PROJECT)
    pm.has_deployments_for_current_cluster = AsyncMock(return_value=True)
    pm._project_file_handler = MagicMock()
    pm._project_file_handler.reenable_components_with_changed_image.return_value = []
    pm.check_and_create_namespaces = AsyncMock()
    pm.check_and_create_sops_secrets_in_namespaces = AsyncMock()
    pm._ensure_database_manager = AsyncMock()
    pm._process_application_manifests = AsyncMock()
    pm.save_and_commit_project = AsyncMock()
    pm._record_reconciliation = AsyncMock()
    pm._argo_manager = MagicMock()
    pm._argo_manager.create_argocd_resources = AsyncMock()
    pm._bootstrap_manager = MagicMock()
    pm._bootstrap_manager.execute_bootstrap_for_deployment = AsyncMock()
    return pm


async def _run_handler_manager(handler: Any, extra_payload: dict[str, Any]) -> MagicMock:
    """Draai een refresh-handler en geef de projectmanager terug die hij gebruikte."""
    manager = _handler_manager(terminated=[], processing_result=True)
    await _drive_handler(handler, manager, extra_payload)
    return manager


async def _run_handler(handler: Any, *, terminated: list[str], processing_result: bool) -> dict[str, Any]:
    """Een refresh-handler met alleen zijn projectmanager en store gemockt."""
    manager = _handler_manager(terminated=terminated, processing_result=processing_result)
    return await _drive_handler(handler, manager, {"reset_running_sync": True})


def _handler_manager(*, terminated: list[str], processing_result: bool) -> MagicMock:
    manager = MagicMock()
    manager.process_project_from_git = AsyncMock(return_value=processing_result)
    manager.get_deployment_results.return_value = {}
    manager.get_component_failures.return_value = None
    manager.get_terminated_sync_operations.return_value = terminated
    manager.get_processing_error.return_value = "iets ging mis"
    manager.close = AsyncMock()
    return manager


async def _drive_handler(handler: Any, manager: MagicMock, extra_payload: dict[str, Any]) -> dict[str, Any]:
    project = MagicMock()
    project.filename = f"{PROJECT}.yaml"
    store = MagicMock()
    store.get.return_value = project
    store.reconcile = AsyncMock()

    progress = MagicMock()
    progress.add_task.return_value = "t"

    with (
        patch("opi.core.task_handlers_operations.get_project_store", return_value=store),
        patch("opi.manager.project_manager.create_project_manager", return_value=manager),
        patch("opi.services.oom_watcher.schedule_oom_check"),
        patch("opi.services.oom_watcher.reset_oom_tune_attempts"),
    ):
        return await handler({"project_name": PROJECT, "deployment_name": DEPLOYMENT, **extra_payload}, progress)
