"""Een sync die niet afbreekt blokkeert elke volgende, dus OPI laat er nooit een staan (RC-250).

Op 5 oktober 2026 wachtte een sync op een Deployment die nooit gezond werd. Hij bleef
daardoor `Running`, en 45 minuten lang werd elke volgende sync overgeslagen met
`Skipping auto-sync: another operation is in progress`. De blokkade heft zich niet zelf op:
de prune-taken die de operation nog moest doen verdwijnen alleen als een sync ze uitvoert,
en die operation is zelf de reden dat geen sync afrondt.

Twee momenten waarop OPI dat nu doorbreekt, en ze zijn niet uitwisselbaar:

* na zijn EIGEN time-out van 300s, want de operation waarop hij stond te wachten laten
  staan maakt van een mislukte uitrol ook een blokkade op elke volgende (die stand duurde
  op 5 oktober na de time-out nog 21 minuten);
* vlak VOOR een push, want wat we pushen moet uitgerold kunnen worden, ook als een andere
  taak of een sweep de blokkade heeft achtergelaten. Dat is tegelijk de uitweg voor de
  gebruiker: `zadctl deployment refresh` loopt langs dit pad.

En het onderscheid dat de beslissing draagt: `.operation` is wat auto-sync tegenhoudt
(`autoSync` begint met `if app.Operation != nil`), `status.operationState` is wat de laatste
operation deed. Een `Running` operationState zonder `.operation` is de losgekoppelde stand
die na een eerdere beëindiging achterblijft, en die blokkeert niets meer.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.kubectl import KubectlConnectionError, KubectlConnector, KubectlExecutionError
from opi.core.cluster_config import get_argo_namespace
from opi.core.config import settings
from opi.core.task_manager import TaskStatus
from opi.manager.project_manager import ProjectManager
from opi.services.argocd_operation import (
    STUCK_OPERATION_THRESHOLD_SECONDS,
    format_duration,
    is_stuck,
    operation_age_seconds,
    read_sync_operation,
    terminate_stuck_operation,
)

# De time-out-tak zit in een closure in een methode van 500 regels en is niet los aan te
# roepen. ``test_verdwenen_applicatie_tijdens_wacht`` heeft daar al een harnas voor dat tot
# aan de ArgoCD-wacht loopt en daar een uitzondering levert; een tweede kopie ernaast zou
# hetzelfde pad op twee plekken moeten bijhouden.
from tests.test_verdwenen_applicatie_tijdens_wacht import _progress, _project_manager

_NOW = datetime(2026, 10, 5, 15, 13, 24, tzinfo=UTC)


def _application(
    *,
    phase: str | None = "Running",
    started_at: str | None = "2026-10-05T14:54:30Z",
    finished_at: str | None = None,
    revision: str | None = "6a0295a671",
    attached: bool = True,
) -> dict:
    """Een Application zoals de incident-Application er om 15:13 uitzag."""
    state: dict = {}
    if phase:
        state["phase"] = phase
    if started_at:
        state["startedAt"] = started_at
    if finished_at:
        state["finishedAt"] = finished_at
    if revision:
        state["syncResult"] = {"revision": revision}
    state["message"] = "waiting for healthy state of apps/Deployment/pr-68-website"
    application: dict = {"metadata": {"name": "dd-mco-pr-68"}, "status": {"operationState": state}}
    if attached:
        application["operation"] = {"sync": {"revision": revision}}
    return application


class TestReadSyncOperation:
    def test_it_reads_the_fields_the_decision_leans_on(self) -> None:
        operation = read_sync_operation(_application())
        assert operation.phase == "Running"
        assert operation.started_at == "2026-10-05T14:54:30Z"
        assert operation.revision == "6a0295a671"
        assert operation.attached is True

    def test_the_uitgerolde_revision_comes_from_sync_result(self) -> None:
        """Niet uit ``status.sync.revision``: dat is de revisie waartegen VERGELEKEN is, en op
        5 oktober waren die twee verschillend (vergelijking op d65d2e8e, operation op
        6a0295a6). Juist dat verschil was het probleem."""
        application = _application(revision="6a0295a671")
        application["status"]["sync"] = {"revision": "d65d2e8eaf"}
        assert read_sync_operation(application).revision == "6a0295a671"

    def test_an_application_without_an_operation_blocks_nothing(self) -> None:
        assert read_sync_operation({"status": {}}).blocks_next_sync is False
        assert read_sync_operation(None).blocks_next_sync is False

    def test_a_running_state_without_the_operation_field_blocks_nothing(self) -> None:
        """De losgekoppelde stand: niets ruimt een ``operationState`` op nadat ``.operation``
        is weggehaald, dus die blijft ``Running`` tot een volgende sync hem overschrijft.
        Auto-sync is dan al vrij, want die kijkt alleen naar ``.operation``."""
        operation = read_sync_operation(_application(attached=False))
        assert operation.ongoing is True
        assert operation.blocks_next_sync is False

    def test_a_finished_operation_blocks_nothing(self) -> None:
        operation = read_sync_operation(
            _application(phase="Succeeded", finished_at="2026-10-05T14:54:33Z", attached=True)
        )
        assert operation.blocks_next_sync is False

    def test_terminating_still_counts_as_ongoing(self) -> None:
        """De controller-grens zet deze phase; de operation staat er dan nog."""
        assert read_sync_operation(_application(phase="Terminating")).blocks_next_sync is True


class TestOperationAge:
    def test_it_counts_from_started_at(self) -> None:
        assert operation_age_seconds(read_sync_operation(_application()), now=_NOW) == 1134

    def test_a_timestamp_without_a_zone_is_read_as_utc(self) -> None:
        operation = read_sync_operation(_application(started_at="2026-10-05T14:54:30"))
        assert operation_age_seconds(operation, now=_NOW) == 1134

    def test_a_missing_or_unreadable_start_is_unknown_and_not_zero(self) -> None:
        """Nul zou 'net begonnen' betekenen, en dat is precies de stand waarin je hem laat
        staan. Onbekend moet de aanroeper apart afhandelen."""
        assert operation_age_seconds(read_sync_operation(_application(started_at=None))) is None
        assert operation_age_seconds(read_sync_operation(_application(started_at="gisteren"))) is None


class TestFormatDuration:
    """De duur die op de kaart en in de deviatieregel staat. Een duur en geen klok, want
    ArgoCD rapporteert in UTC en OPI's eigen regels in CEST, maar dan moet de duur zelf wel
    kloppen: "loopt al 1 minuten" of een "2 uur" dat 61 minuten is, maakt precies de vraag
    onbeantwoord waarvoor hij er staat ("is dit lang?")."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            # Een negatieve duur bij klokverschil tussen ArgoCD (UTC) en OPI (CEST): "loopt al
            # -89 seconden" is een onmogelijk getal op het scherm.
            (-89, "0 seconden"),
            (-1, "0 seconden"),
            (0, "0 seconden"),
            (1, "1 seconde"),
            (40, "40 seconden"),
            (59, "59 seconden"),
            (60, "1 minuut"),
            # Naar BENEDEN, zoals ``relatieve_tijd`` in ``opi/web/lotc_switch.py``. Naar boven
            # zou de melding langer maken dan de sync werkelijk liep.
            (119, "1 minuut"),
            (120, "2 minuten"),
            (1134, "18 minuten"),
            (3599, "59 minuten"),
            (3600, "1 uur"),
            (7140, "1 uur"),
            (7200, "2 uur"),
        ],
    )
    def test_it_rounds_down_and_keeps_the_unit_singular_at_one(self, seconds: int, expected: str) -> None:
        assert format_duration(seconds) == expected

    def test_an_unknown_age_says_so_instead_of_claiming_zero(self) -> None:
        """``operation_age_seconds`` geeft ``None`` bij een ontbrekende of onleesbare
        ``startedAt``; "0 seconden" zou daar "net begonnen" van maken."""
        assert format_duration(None) == "onbekend lang"


class TestIsStuck:
    def test_an_operation_past_the_threshold_is_stuck(self) -> None:
        assert is_stuck(read_sync_operation(_application()), threshold_seconds=300, now=_NOW) is True

    def test_an_operation_that_is_still_just_busy_is_not(self) -> None:
        operation = read_sync_operation(_application(started_at="2026-10-05T15:11:00Z"))
        assert operation_age_seconds(operation, now=_NOW) == 144
        assert is_stuck(operation, threshold_seconds=300, now=_NOW) is False

    def test_the_threshold_is_inclusive(self) -> None:
        operation = read_sync_operation(_application(started_at="2026-10-05T15:08:24Z"))
        assert operation_age_seconds(operation, now=_NOW) == 300
        assert is_stuck(operation, threshold_seconds=300, now=_NOW) is True

    def test_an_unknown_age_on_a_blocking_operation_counts_as_stuck(self) -> None:
        """Een aangehechte operation zonder leesbaar begin is de stand waar niets meer aan
        verandert; 'leeftijd onbekend' mag geen vrijbrief zijn om te blijven staan."""
        assert is_stuck(read_sync_operation(_application(started_at=None)), threshold_seconds=300) is True

    def test_a_start_in_the_future_still_counts_as_stuck_on_a_threshold_of_zero(self) -> None:
        """ArgoCD rapporteert in UTC en OPI's eigen regels staan in CEST, dus bij klokverschil
        ligt ``startedAt`` in de toekomst en is de leeftijd negatief (gemeten: -89 seconden).
        Een drempel van 0 betekent "altijd zolang hij blokkeert", en zonder de klem deed hij
        dat juist niet: nul patch-aanroepen op de tak die de blokkade moet opruimen."""
        operation = read_sync_operation(_application(started_at="2026-10-05T15:14:53Z"))
        assert operation_age_seconds(operation, now=_NOW) == -89
        assert is_stuck(operation, threshold_seconds=0, now=_NOW) is True

    def test_the_clamp_leaves_the_threshold_of_300_alone(self) -> None:
        """De tegenproef: wie naar een uitrol van iemand anders kijkt, breekt een operation die
        volgens de klok net begonnen is niet af."""
        operation = read_sync_operation(_application(started_at="2026-10-05T15:14:53Z"))
        assert is_stuck(operation, threshold_seconds=300, now=_NOW) is False

    def test_a_finished_operation_is_never_stuck_however_old(self) -> None:
        operation = read_sync_operation(
            _application(phase="Succeeded", started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:03Z")
        )
        assert is_stuck(operation, threshold_seconds=300, now=_NOW) is False

    def test_the_threshold_equals_opis_own_wait(self) -> None:
        """Lager zou een uitrol kunnen afbreken die nog gewoon bezig is: OPI wacht zelf 300s
        voordat hij een uitrol opgeeft. Dat geldt voor de ronde VOOR de push en voor het
        rapport; op OPI's eigen time-out-tak is "nog gewoon bezig" geen mogelijkheid meer, en
        die geeft daarom 0 mee (zie ``TestOpisOwnTimeOut``)."""
        assert STUCK_OPERATION_THRESHOLD_SECONDS == 300


class TestGetApplication:
    """De lezer in de connector. ``terminate_stuck_operation`` beslist op wat hier uitkomt,
    dus een mislukte read die als lege Application terugkomt zou een blokkade wegpoetsen in
    plaats van hem te vinden."""

    @pytest.fixture
    def connector(self) -> KubectlConnector:
        with patch("opi.connectors.kubectl.asyncio.create_task", new=MagicMock()):
            conn = KubectlConnector()
        KubectlConnector.isConnected = True
        return conn

    @pytest.mark.asyncio
    async def test_it_asks_for_the_whole_application_as_json(self, connector: KubectlConnector) -> None:
        """``-o json`` en niet een jsonpath: deze lezer heeft zowel ``.operation`` als
        ``status.operationState`` nodig, en die twee uit elkaar houden is de hele beslissing."""
        run = AsyncMock(return_value=('{"metadata": {"name": "dd-mco-pr-68"}}', "", 0))
        with patch.object(connector, "_run_kubectl_command", run):
            application = await connector.get_argocd_application("dd-mco-pr-68", "argocd")

        assert application == {"metadata": {"name": "dd-mco-pr-68"}}
        assert run.await_args.args[0] == ["get", "application", "dd-mco-pr-68", "-n", "argocd", "-o", "json"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("cluster", "namespace"),
        [("odcn-production", "rig-prd-operations"), ("sandboxed-local", "rig-system")],
    )
    async def test_without_a_namespace_it_uses_the_argo_namespace_of_this_cluster(
        self, connector: KubectlConnector, monkeypatch: pytest.MonkeyPatch, cluster: str, namespace: str
    ) -> None:
        """Twee clusters met een verschillende argo-namespace, met de verwachting als letterlijke
        waarde: dezelfde expressie als de code hier herhalen zou ook groen blijven op een vaste
        ``"argocd"``."""
        monkeypatch.setattr(settings, "CLUSTER_MANAGER", cluster)
        run = AsyncMock(return_value=("{}", "", 0))
        with patch.object(connector, "_run_kubectl_command", run):
            await connector.get_argocd_application("dd-mco-pr-68")

        args = run.await_args.args[0]
        assert args[args.index("-n") + 1] == namespace

    @pytest.mark.asyncio
    async def test_an_application_that_does_not_exist_reads_as_nothing(self, connector: KubectlConnector) -> None:
        stderr = 'Error from server (NotFound): applications.argoproj.io "dd-mco-pr-68" not found'
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", stderr, 1))):
            assert await connector.get_argocd_application("dd-mco-pr-68", "argocd") is None

    @pytest.mark.asyncio
    async def test_a_failed_read_reads_as_nothing_too(self, connector: KubectlConnector) -> None:
        """ "Bestaat niet" en "kon niet gelezen worden" geven hetzelfde antwoord, en dat is
        veilig: de aanroeper patcht dan niets. Een lege dict teruggeven zou van beide
        "geen blokkade" maken."""
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", "connection reset", 1))):
            assert await connector.get_argocd_application("dd-mco-pr-68", "argocd") is None

    @pytest.mark.asyncio
    async def test_unparseable_output_does_not_raise(self, connector: KubectlConnector) -> None:
        """kubectl kan met code 0 iets anders dan JSON opleveren (een waarschuwing op stdout,
        een afgekapte stream). Dit loopt in het pad vlak voor elke push, dus een
        ``JSONDecodeError`` hier zou een uitrol tegenhouden om een leesfout."""
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("niet json", "", 0))):
            assert await connector.get_argocd_application("dd-mco-pr-68", "argocd") is None

    @pytest.mark.asyncio
    async def test_json_that_is_not_an_object_reads_as_nothing(self, connector: KubectlConnector) -> None:
        """``-o json`` op een lijst-selectie levert geldige JSON die geen Application is.
        ``read_sync_operation`` zou daarop op ``.get`` stuklopen."""
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("[]", "", 0))):
            assert await connector.get_argocd_application("dd-mco-pr-68", "argocd") is None


class TestTerminateStuckOperation:
    @pytest.fixture
    def kubectl(self) -> MagicMock:
        connector = MagicMock()
        connector.get_argocd_application = AsyncMock(return_value=_application())
        connector.terminate_argocd_application_operation = AsyncMock(return_value=True)
        return connector

    @pytest.mark.asyncio
    async def test_a_stuck_operation_is_terminated_in_its_own_namespace(self, kubectl: MagicMock) -> None:
        assert await terminate_stuck_operation(kubectl, "dd-mco-pr-68", "argocd") is True
        kubectl.get_argocd_application.assert_awaited_once_with("dd-mco-pr-68", "argocd")
        kubectl.terminate_argocd_application_operation.assert_awaited_once_with("dd-mco-pr-68", "argocd")

    @pytest.mark.asyncio
    async def test_a_young_operation_is_left_alone(self, kubectl: MagicMock) -> None:
        kubectl.get_argocd_application.return_value = _application(
            started_at=datetime.now(UTC).isoformat().replace("+00:00", "Z")
        )
        assert await terminate_stuck_operation(kubectl, "dd-mco-pr-68", "argocd") is False
        kubectl.terminate_argocd_application_operation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_finished_operation_is_left_alone(self, kubectl: MagicMock) -> None:
        kubectl.get_argocd_application.return_value = _application(
            phase="Succeeded", finished_at="2026-10-05T14:54:33Z"
        )
        assert await terminate_stuck_operation(kubectl, "dd-mco-pr-68", "argocd") is False
        kubectl.terminate_argocd_application_operation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_detached_running_state_is_left_alone(self, kubectl: MagicMock) -> None:
        """Hier valt niets te beëindigen: ``.operation`` is al weg, dus auto-sync is vrij. Een
        patch zou alleen een 'missing path' opleveren."""
        kubectl.get_argocd_application.return_value = _application(attached=False)
        assert await terminate_stuck_operation(kubectl, "dd-mco-pr-68", "argocd") is False
        kubectl.terminate_argocd_application_operation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unreadable_application_is_not_patched(self, kubectl: MagicMock) -> None:
        """``None`` dekt zowel 'bestaat niet' als 'kon niet gelezen worden'. Blind patchen op
        een mislukte read maakt van een onbekende stand een mutatie."""
        kubectl.get_argocd_application.return_value = None
        assert await terminate_stuck_operation(kubectl, "dd-mco-pr-68", "argocd") is False
        kubectl.terminate_argocd_application_operation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_patch_is_reported_as_not_terminated(self, kubectl: MagicMock) -> None:
        kubectl.terminate_argocd_application_operation.return_value = False
        assert await terminate_stuck_operation(kubectl, "dd-mco-pr-68", "argocd") is False

    @pytest.mark.asyncio
    async def test_an_unreachable_kubectl_is_not_an_exception_for_the_caller(self, kubectl: MagicMock) -> None:
        """Beide aanroepers staan op een pad dat niet mag stranden, en deze tak is niet
        zeldzaam: ``_run_kubectl_command`` gooit zodra ``KubectlConnector.isConnected`` False
        is, en die vlag gaat procesbreed om bij een enkele "connection refused"."""
        kubectl.get_argocd_application = AsyncMock(
            side_effect=KubectlConnectionError("kubectl connection failed: connection refused")
        )
        assert await terminate_stuck_operation(kubectl, "dd-mco-pr-68", "argocd") is False
        kubectl.terminate_argocd_application_operation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_patch_that_cannot_run_is_not_an_exception_either(self, kubectl: MagicMock) -> None:
        """De tweede kubectl-aanroep van deze functie, met dezelfde uitkomst: niet beëindigd,
        en de aanroeper loopt door."""
        kubectl.terminate_argocd_application_operation = AsyncMock(
            side_effect=KubectlExecutionError("kubectl command timed out after 60s")
        )
        assert await terminate_stuck_operation(kubectl, "dd-mco-pr-68", "argocd") is False

    @pytest.mark.asyncio
    async def test_anything_that_is_not_a_kubectl_failure_still_reaches_the_caller(self, kubectl: MagicMock) -> None:
        """De tegenproef op de twee hierboven: best-effort geldt voor een kubectl die weg is,
        niet voor elke fout. Verbreden naar ``except Exception`` gaf 0 rood over de selectie,
        en dan meldt deze functie "niet beëindigd" terwijl de echte oorzaak een fout in OPI
        zelf is."""
        kubectl.get_argocd_application = AsyncMock(side_effect=ValueError("onverwachte payload"))
        with pytest.raises(ValueError, match="onverwachte payload"):
            await terminate_stuck_operation(kubectl, "dd-mco-pr-68", "argocd")


class TestClearBlockingSyncOperations:
    @pytest.fixture
    def manager(self) -> MagicMock:
        fake = MagicMock(spec=ProjectManager)
        fake.get_name = AsyncMock(return_value="dd-mco")
        fake._kubectl_connector = MagicMock()
        fake._kubectl_connector.get_argocd_application = AsyncMock(return_value=_application())
        fake._kubectl_connector.terminate_argocd_application_operation = AsyncMock(return_value=True)
        # Instance-attribuut, dus ``spec=ProjectManager`` levert hem niet.
        fake._terminated_sync_operations = []
        return fake

    @pytest.mark.asyncio
    async def test_it_asks_for_the_application_name_the_deployment_actually_has(self, manager: MagicMock) -> None:
        """De applicatienaam en de argo-namespace komen uit dezelfde twee functies die de
        Application aanmaken, dus een naam die hier niet bestaat kan niet ongemerkt."""
        await ProjectManager._clear_blocking_sync_operations(manager, [{"name": "pr-68", "cluster": "odcn-production"}])
        manager._kubectl_connector.get_argocd_application.assert_awaited_once_with("dd-mco-pr-68", "rig-prd-operations")
        manager._kubectl_connector.terminate_argocd_application_operation.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_every_deployment_in_the_repository_is_covered(self, manager: MagicMock) -> None:
        await ProjectManager._clear_blocking_sync_operations(
            manager,
            [
                {"name": "pr-68", "cluster": "odcn-production"},
                {"name": "productie", "cluster": "odcn-production"},
            ],
        )
        assert [call.args[0] for call in manager._kubectl_connector.get_argocd_application.await_args_list] == [
            "dd-mco-pr-68",
            "dd-mco-productie",
        ]

    @pytest.mark.asyncio
    async def test_one_deployment_that_cannot_be_freed_does_not_hold_up_the_rest(self, manager: MagicMock) -> None:
        """Dit loopt vlak voor de push, dus een mislukte patch mag de uitrol niet tegenhouden
        en mag ook de tweede deployment niet overslaan. De patch die niet lukte is al gelogd;
        hier gaat het erom dat de rij doorloopt."""
        manager._kubectl_connector.terminate_argocd_application_operation = AsyncMock(side_effect=[False, True])
        await ProjectManager._clear_blocking_sync_operations(
            manager,
            [{"name": "pr-68", "cluster": "odcn-production"}, {"name": "productie", "cluster": "odcn-production"}],
        )
        assert [call.args[0] for call in manager._kubectl_connector.get_argocd_application.await_args_list] == [
            "dd-mco-pr-68",
            "dd-mco-productie",
        ]

    @pytest.mark.asyncio
    async def test_an_unreachable_kubectl_does_not_skip_the_rest_either(self, manager: MagicMock) -> None:
        """Dezelfde eis als hierboven, maar met een omgevallen kubectl in plaats van een
        mislukte patch. Een procesbrede verbindingsvlag maakt dit de vorm die bij een hik van
        de apiserver optreedt, en dan moet de rij nog steeds doorlopen."""
        manager._kubectl_connector.get_argocd_application = AsyncMock(
            side_effect=[KubectlConnectionError("connection refused"), _application()]
        )
        await ProjectManager._clear_blocking_sync_operations(
            manager,
            [{"name": "pr-68", "cluster": "odcn-production"}, {"name": "productie", "cluster": "odcn-production"}],
        )
        assert [call.args[0] for call in manager._kubectl_connector.get_argocd_application.await_args_list] == [
            "dd-mco-pr-68",
            "dd-mco-productie",
        ]
        manager._kubectl_connector.terminate_argocd_application_operation.assert_awaited_once_with(
            "dd-mco-productie", "rig-prd-operations"
        )

    @pytest.mark.asyncio
    async def test_a_project_without_a_name_touches_no_application(self, manager: MagicMock) -> None:
        """Zonder projectnaam is er geen applicatienaam te maken. Gemeten:
        ``generate_argocd_application_name(None, "pr-68")`` loopt stuk op een ``AttributeError``
        en met ``""`` levert hij ``-pr-68``, een Application die van geen enkel project is. Dit
        pad loopt vlak voor elke push, dus beide zijn daar duur."""
        manager.get_name = AsyncMock(return_value=None)
        await ProjectManager._clear_blocking_sync_operations(manager, [{"name": "pr-68", "cluster": "odcn-production"}])
        manager._kubectl_connector.get_argocd_application.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_deployment_without_a_name_or_cluster_is_skipped(self, manager: MagicMock) -> None:
        """Zonder die twee is er geen Application om naar te kijken, en een gegokte naam zou
        een Application van iemand anders kunnen raken."""
        await ProjectManager._clear_blocking_sync_operations(
            manager, [{"cluster": "odcn-production"}, {"name": "pr-68"}]
        )
        manager._kubectl_connector.get_argocd_application.assert_not_awaited()


class TestOpisOwnTimeOut:
    """De tak na OPI's eigen 300s wacht, en die beëindigt ZONDER drempel.

    De operation waarop die tak stond te wachten is per constructie jonger dan de drempel:
    ``wait_for_application_synced`` start zijn teller pas na ``refresh_application`` en
    ``get_application_manifests``, en ArgoCD begint de operation daarna. Met de
    standaarddrempel bleef precies de blokkade staan die deze tak moet opruimen, gemeten op
    deze branch: 250/280/295/299s oud gaf nul patch-aanroepen.
    """

    @pytest.fixture
    def manager(self) -> MagicMock:
        fake = MagicMock(spec=ProjectManager)
        fake._kubectl_connector = MagicMock()
        started = (datetime.now(UTC) - timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
        fake._kubectl_connector.get_argocd_application = AsyncMock(return_value=_application(started_at=started))
        fake._kubectl_connector.terminate_argocd_application_operation = AsyncMock(return_value=True)
        return fake

    @pytest.mark.asyncio
    @pytest.mark.parametrize("age_seconds", [0, 1, 120, 299])
    async def test_an_operation_younger_than_the_threshold_is_still_terminated(
        self, manager: MagicMock, age_seconds: int
    ) -> None:
        """Elke leeftijd onder de drempel, want "nog gewoon bezig" is hier geen mogelijkheid
        meer: OPI heeft net 300s op deze operation gewacht en opgegeven.

        Nul en een seconde horen erbij en niet alleen de 299 die de review mat. ArgoCD begint
        de operation pas op zijn eerstvolgende reconcile, dus die kan op het moment van de
        time-out seconden oud zijn, en dat is precies de stand die een drempel van 'laag maar
        niet nul' alsnog zou laten staan.
        """
        started = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat().replace("+00:00", "Z")
        manager._kubectl_connector.get_argocd_application.return_value = _application(started_at=started)
        await ProjectManager._terminate_operation_we_waited_on(manager, "dd-mco-pr-68", "odcn-production")
        manager._kubectl_connector.terminate_argocd_application_operation.assert_awaited_once_with(
            "dd-mco-pr-68", "rig-prd-operations"
        )

    @pytest.mark.asyncio
    async def test_a_start_in_the_future_is_terminated_too(self, manager: MagicMock) -> None:
        """De drempel van 0 moet "altijd zolang hij blokkeert" betekenen, ook bij klokverschil
        tussen ArgoCD (UTC) en OPI (CEST). Gemeten met een ``startedAt`` 90 seconden in de
        toekomst: leeftijd -89, en zonder de klem in ``is_stuck`` nul patch-aanroepen."""
        started = (datetime.now(UTC) + timedelta(seconds=90)).isoformat().replace("+00:00", "Z")
        manager._kubectl_connector.get_argocd_application.return_value = _application(started_at=started)
        await ProjectManager._terminate_operation_we_waited_on(manager, "dd-mco-pr-68", "odcn-production")
        manager._kubectl_connector.terminate_argocd_application_operation.assert_awaited_once_with(
            "dd-mco-pr-68", "rig-prd-operations"
        )

    @pytest.mark.asyncio
    async def test_a_detached_state_is_still_left_alone(self, manager: MagicMock) -> None:
        """Zonder drempel blijft ``blocks_next_sync`` de enige grendel, en die hoort te houden:
        ``.operation`` is hier al weg, dus auto-sync is vrij en een patch levert alleen een
        'missing path'."""
        manager._kubectl_connector.get_argocd_application.return_value = _application(attached=False)
        await ProjectManager._terminate_operation_we_waited_on(manager, "dd-mco-pr-68", "odcn-production")
        manager._kubectl_connector.terminate_argocd_application_operation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_without_a_cluster_it_falls_back_to_the_cluster_opi_manages(self, manager: MagicMock) -> None:
        """De deployment hoeft geen cluster te noemen; dan is het die van deze OPI-instantie.
        Een verkeerde namespace zou de Application niet vinden en de blokkade laten staan."""
        await ProjectManager._terminate_operation_we_waited_on(manager, "dd-mco-pr-68", None)
        manager._kubectl_connector.get_argocd_application.assert_awaited_once_with(
            "dd-mco-pr-68", get_argo_namespace(settings.CLUSTER_MANAGER)
        )


class TestItRunsBeforeThePush:
    """Dekking op de helper pint de AANROEP niet. Deze toetsen doen dat per plek."""

    @pytest.mark.asyncio
    async def test_the_manifest_path_clears_the_blockade_before_it_pushes(self) -> None:
        """De orde is het hele punt: na de push is de nieuwe revisie al in git terechtgekomen
        terwijl de sync die hem moet uitrollen nog geblokkeerd is."""
        order: list[str] = []
        git = MagicMock()
        git.get_working_dir = AsyncMock(return_value="/tmp/repo")
        git.commit_changes = AsyncMock(side_effect=lambda *a: order.append("commit"))
        git.push_changes = AsyncMock(side_effect=lambda *a: order.append("push"))

        fake = MagicMock(spec=ProjectManager)
        fake.get_contents = AsyncMock(return_value={"name": "dd-mco"})
        fake.get_git_connector_for_deployment = AsyncMock(return_value=git)
        fake._process_deployment_manifests = AsyncMock(side_effect=lambda *a: order.append("manifests"))
        fake.get_deployments = AsyncMock(return_value=[])
        fake._clear_blocking_sync_operations = AsyncMock(side_effect=lambda *a, **kw: order.append("clear"))

        deployments = [{"name": "pr-68", "cluster": "odcn-production", "namespace": "dd-mco"}]
        await ProjectManager._process_repository_manifests(fake, {"name": "main-repo"}, deployments)

        assert order == ["manifests", "commit", "clear", "push"]
        fake._clear_blocking_sync_operations.assert_awaited_once_with(deployments, reset_running_sync=False)

    @pytest.mark.asyncio
    async def test_an_unreachable_kubectl_does_not_hold_up_the_push(self) -> None:
        """De opruimronde staat TUSSEN de commits en de push, dus stranden laat de manifests
        gecommit en ongepusht achter, en de uitzondering loopt door
        ``_process_application_manifests`` heen zodat de repositories daarna ook overgeslagen
        worden. Gemeten voor de reparatie: ``RAISED KubectlConnectionError``, order
        ``['manifests', 'commit']``, push awaited 0. Precies in de situatie waarvoor deze
        feature bestaat, want een hik van de apiserver zet ``isConnected`` procesbreed op
        False."""
        order: list[str] = []
        git = MagicMock()
        git.get_working_dir = AsyncMock(return_value="/tmp/repo")
        git.commit_changes = AsyncMock(side_effect=lambda *a: order.append("commit"))
        git.push_changes = AsyncMock(side_effect=lambda *a: order.append("push"))

        fake = MagicMock(spec=ProjectManager)
        fake.get_contents = AsyncMock(return_value={"name": "dd-mco"})
        fake.get_git_connector_for_deployment = AsyncMock(return_value=git)
        fake._process_deployment_manifests = AsyncMock(side_effect=lambda *a: order.append("manifests"))
        fake.get_deployments = AsyncMock(return_value=[])
        fake.get_name = AsyncMock(return_value="dd-mco")
        fake._kubectl_connector = MagicMock()
        fake._kubectl_connector.get_argocd_application = AsyncMock(
            side_effect=KubectlConnectionError("kubectl connection failed: connection refused")
        )
        # De echte opruimronde en niet een mock ervan: de grendel zit in
        # ``terminate_stuck_operation``, dus een mock hier zou de reparatie wegnemen.
        fake._terminated_sync_operations = []
        fake._clear_blocking_sync_operations = lambda deployments, reset_running_sync: (
            ProjectManager._clear_blocking_sync_operations(fake, deployments, reset_running_sync)
        )

        await ProjectManager._process_repository_manifests(
            fake, {"name": "main-repo"}, [{"name": "pr-68", "cluster": "odcn-production", "namespace": "dd-mco"}]
        )

        assert order == ["manifests", "commit", "push"]
        git.push_changes.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_unreachable_kubectl_still_reports_the_time_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Op de time-out-tak staat de opruiming VOOR ``fail_subtask`` en voor de
        ``timeout``-uitkomst. Een omgevallen connector mag daar geen
        ``KubectlConnectionError`` van maken: de time-out die er werkelijk was hoort gemeld te
        worden, met de deployment als mislukt in de voortgang."""
        manager = _project_manager(monkeypatch, wacht_fout=TimeoutError())
        monkeypatch.setattr(
            manager._kubectl_connector,
            "get_argocd_application",
            AsyncMock(side_effect=KubectlConnectionError("kubectl connection failed: connection refused")),
            raising=False,
        )
        progress = _progress()

        geslaagd = await manager.process_project_from_git("projects/mpfb-8wh.yaml", task_progress_manager=progress)

        assert geslaagd is False
        assert "timed out after 300s waiting for sync" in (manager.get_processing_error() or "")
        deployment = next(info for info in progress._subtasks.values() if info["name"] == "pr-244")
        assert deployment["status"] == TaskStatus.FAILED.value
        assert deployment["error"] == "time-out na 300s"

    def test_opis_own_time_out_terminates_the_operation_it_waited_on(self) -> None:
        """De time-out-tak zit in een closure binnen een methode van 500 regels, dus hij is
        niet los aan te roepen. Daarom op de AST: de aanroep moet IN de
        ``except TimeoutError``-handler van ``_refresh_and_wait`` staan. Eromheen, bijvoorbeeld
        in het succespad, zou deze toets groen laten terwijl de blokkade blijft staan."""
        source = inspect.getsource(ProjectManager.process_project_from_git)
        tree = ast.parse(Path(inspect.getsourcefile(ProjectManager) or "").read_text())

        handlers: list[ast.ExceptHandler] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_refresh_and_wait":
                handlers = [
                    handler
                    for handler in ast.walk(node)
                    if isinstance(handler, ast.ExceptHandler)
                    and isinstance(handler.type, ast.Name)
                    and handler.type.id == "TimeoutError"
                ]
        assert len(handlers) == 1, "geen (of meer dan een) except TimeoutError in _refresh_and_wait"

        called = {
            call.func.attr
            for call in ast.walk(handlers[0])
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        }
        assert "_terminate_operation_we_waited_on" in called
        assert "_terminate_operation_we_waited_on" in source
