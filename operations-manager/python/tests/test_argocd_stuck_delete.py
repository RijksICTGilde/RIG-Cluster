"""RC-226: a delete that hangs, and the forcing that leaves rubbish behind.

Two things have to be true of the delete route, and both are about ORDER:

1. The running operation goes FIRST. A sync that waits on health blocks the deletion for
   as long as it runs, and it runs forever when the workload it waits for cannot become
   healthy. Only clearing ``/operation`` releases it.
2. When forcing, the resources go before the finalizer. The finalizer IS the cascade that
   deletes them; taking it away first is what left 350 resources behind in
   rig-prd-mpfm-w3h.

So these tests assert sequences, not just that the calls happened.
"""

import asyncio
import json
import logging
import time
from inspect import signature
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.argo import ArgoConnector
from opi.connectors.kubectl import KubectlConnector, types_named_in_kubectl_error
from opi.manager.delete_project_manager import (
    _DELETION_PROGRESS_DEADLINE,
    CASCADE_WAIT_DELAY,
    CASCADE_WAIT_RETRIES,
    DELETION_PROGRESS_BUDGET_SECONDS,
    DeleteProjectManager,
    ForcedDeletion,
    deletion_progress_window,
)
from opi.utils.argocd_tracking import (
    INSTANCE_LABEL,
    LABEL_VALUE_MAX,
    TRACKING_ID_ANNOTATION,
    TrackedResource,
    application_name_from_tracking_id,
    may_be_cut_from,
    tracked_resource_from_item,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    KubectlConnector._instance = None
    KubectlConnector._initialized = False
    yield
    KubectlConnector._instance = None
    KubectlConnector._initialized = False


@pytest.fixture
def connector():
    with patch("opi.connectors.kubectl.asyncio.create_task", new=MagicMock()):
        conn = KubectlConnector()
    KubectlConnector.isConnected = True
    return conn


def _item(
    kind: str, api_version: str, name: str, tracking_id: str | None, instance_label: str | None = None, **metadata
) -> dict:
    annotations = {TRACKING_ID_ANNOTATION: tracking_id} if tracking_id else {}
    labels = {INSTANCE_LABEL: instance_label} if instance_label else {}
    return {
        "kind": kind,
        "apiVersion": api_version,
        "metadata": {
            "name": name,
            "namespace": "rig-prd-mpfm-w3h",
            "annotations": annotations,
            "labels": labels,
            **metadata,
        },
    }


# ---------------------------------------------------------------------------
# The tracking-id annotation: the only link from a resource back to its Application
# ---------------------------------------------------------------------------


class TestTrackingId:
    def test_application_name_is_the_part_before_the_first_colon(self) -> None:
        assert application_name_from_tracking_id("mpfm-w3h-pr-310:apps/Deployment:ns/pr-310-magazijna") == (
            "mpfm-w3h-pr-310"
        )

    def test_app_in_any_namespace_form_drops_the_namespace(self) -> None:
        """With apps-in-any-namespace the app part is ``<ns>/<app>``; the name is the tail."""
        assert application_name_from_tracking_id("argocd/mpfm-w3h-pr-310:apps/Deployment:ns/x") == "mpfm-w3h-pr-310"

    def test_empty_tracking_id_has_no_application(self) -> None:
        assert application_name_from_tracking_id("") is None
        assert application_name_from_tracking_id(":apps/Deployment:ns/x") is None

    def test_untracked_resource_is_skipped(self) -> None:
        assert tracked_resource_from_item(_item("Secret", "v1", "db-creds", None)) is None

    def test_tracked_resource_reads_kind_name_and_owner(self) -> None:
        resource = tracked_resource_from_item(
            _item("Secret", "v1", "db-creds", "app-a:/Secret:ns/db-creds", deletionTimestamp="2026-09-24T10:00:00Z")
        )
        assert resource is not None
        assert (resource.kind, resource.name, resource.app_name) == ("Secret", "db-creds", "app-a")

    def test_the_instance_label_also_names_the_owner(self) -> None:
        """ArgoCD's DEFAULT tracking method, which is what local and sandboxed-local run.

        Measured on the sandbox cluster on 24 September 2026: argocd-cm says
        resourceTrackingMethod=label, and not one resource under a live Application
        carried a tracking-id. Reading only the annotation finds nothing there.
        """
        resource = tracked_resource_from_item(_item("Secret", "v1", "db-creds", None, instance_label="app-a"))
        assert resource is not None
        assert (resource.app_name, resource.from_label) == ("app-a", True)

    def test_an_owned_resource_is_its_owners(self) -> None:
        """Measured on odcn-production, 9 October 2026: CNPG copies the instance label onto
        the pod and services of the live database algor-odc-db, and the sweep reported
        them as the orphans of an Application by that name. ``--delete`` takes them."""
        owned = _item(
            "Pod",
            "v1",
            "algor-odc-db-1",
            None,
            instance_label="algor-odc-db",
            ownerReferences=[{"kind": "Cluster", "name": "algor-odc-db"}],
        )
        assert tracked_resource_from_item(owned) is None

    def test_the_annotation_wins_over_a_label(self) -> None:
        """With annotation tracking the label may be a leftover from another tool."""
        resource = tracked_resource_from_item(
            _item("Secret", "v1", "db-creds", "app-a:/Secret:ns/db-creds", instance_label="something-else")
        )
        assert resource is not None
        assert (resource.app_name, resource.from_label) == ("app-a", False)

    def test_a_malformed_annotation_does_not_fall_back_to_the_label(self) -> None:
        """It is the annotation's ABSENCE that hands over to the label, not its emptiness.

        On an annotation cluster a Helm release writes ``instance`` too, and there it names
        the release. Falling back would let it name the owner of a resource ArgoCD tracks by
        annotation, and what follows a claim is a delete.
        """
        item = _item("Secret", "v1", "db-creds", ":apps/Deployment:ns/x", instance_label="ingress-nginx")
        assert tracked_resource_from_item(item) is None

    def test_an_empty_label_owns_nothing(self) -> None:
        resource = tracked_resource_from_item(_item("Secret", "v1", "db-creds", None, instance_label="   "))
        assert resource is None

    def test_a_resource_without_a_name_is_claimed_by_nobody(self) -> None:
        """What follows a claim is a delete, and a delete needs something to aim at."""
        assert tracked_resource_from_item(_item("Secret", "v1", "", "app-a:/Secret:ns/x")) is None

    def test_kubectl_type_carries_the_api_group(self) -> None:
        deployment = tracked_resource_from_item(_item("Deployment", "apps/v1", "web", "app-a:apps/Deployment:ns/web"))
        secret = tracked_resource_from_item(_item("Secret", "v1", "db-creds", "app-a:/Secret:ns/db-creds"))
        assert deployment is not None
        assert deployment.kubectl_type == "deployment.apps"
        assert secret is not None
        assert secret.kubectl_type == "secret"


class TestMayBeCutFrom:
    """A mark sitting exactly on the label cap may be a longer name cut to it.

    That, and nothing more: the answer is 'undecidable', not 'owned'. What the two callers
    then do with an undecidable mark is the opposite of each other, which is why it is not
    a predicate about belonging (see may_be_cut_from).
    """

    #: A name longer than a label value can hold, in the shape the schema allows.
    LONG_APP = "a" * 30 + "-" + "b" * 63

    @staticmethod
    def _labelled(app_name: str) -> TrackedResource:
        """A resource as a LABEL cluster marks it, read the way the connector reads it."""
        resource = tracked_resource_from_item(_item("Secret", "v1", "db-creds", None, instance_label=app_name))
        assert resource is not None
        return resource

    @staticmethod
    def _annotated(app_name: str) -> TrackedResource:
        resource = tracked_resource_from_item(_item("Secret", "v1", "db-creds", f"{app_name}:/Secret:ns/db-creds"))
        assert resource is not None
        return resource

    def test_a_label_at_the_cap_may_be_the_longer_name_it_starts(self) -> None:
        assert len(self.LONG_APP) > LABEL_VALUE_MAX
        assert may_be_cut_from(self._labelled(self.LONG_APP[:LABEL_VALUE_MAX]), {self.LONG_APP}) is True

    def test_an_annotation_is_never_a_cut_name(self) -> None:
        """Only the label has the cap. odcn-production is the one cluster type that tracks by
        annotation, so there nothing is ever undecidable and every comparison is on the whole
        name, which is also the cluster this task is about."""
        assert may_be_cut_from(self._annotated(self.LONG_APP[:LABEL_VALUE_MAX]), {self.LONG_APP}) is False

    def test_a_mark_that_IS_one_of_the_names_is_still_ambiguous(self) -> None:
        """Equality settles nothing here: ``LONG_APP`` is running too, and on a label cluster
        every resource of it carries this same value. Answering False let the force delete a
        living neighbour's PVC and report success."""
        cut = self.LONG_APP[:LABEL_VALUE_MAX]
        assert may_be_cut_from(self._labelled(cut), {cut, self.LONG_APP}) is True

    def test_the_mark_itself_is_not_a_name_it_could_be_cut_from(self) -> None:
        """The only name that cannot make a mark ambiguous. Without this exclusion every mark
        at the cap would be undecidable against its own Application and the force would delete
        nothing."""
        cut = self.LONG_APP[:LABEL_VALUE_MAX]
        assert may_be_cut_from(self._labelled(cut), {cut}) is False

    def test_a_mark_at_the_cap_still_needs_a_name_it_could_be_cut_from(self) -> None:
        """Otherwise the cap would make every long orphan undecidable and nothing would ever
        be reported as one."""
        assert may_be_cut_from(self._labelled("z" * LABEL_VALUE_MAX), {self.LONG_APP}) is False

    def test_a_mark_below_the_cap_was_never_cut(self) -> None:
        """``mpfm-w3h-pr-31`` fits, so it is a whole name and ``mpfm-w3h-pr-310`` says nothing
        about it."""
        assert may_be_cut_from(self._labelled("mpfm-w3h-pr-31"), {"mpfm-w3h-pr-310"}) is False

    def test_a_mark_over_the_cap_was_never_cut(self) -> None:
        assert may_be_cut_from(self._labelled(self.LONG_APP[:-1]), {self.LONG_APP}) is False


# ---------------------------------------------------------------------------
# The connector: the manual repair that worked, as code
# ---------------------------------------------------------------------------


class TestTerminateOperation:
    #: What ``kubectl get -o jsonpath={.operation}`` prints for an Application that has one.
    #: An absent object field prints nothing at exit 0, a present one its JSON; measured on
    #: the sandbox apiserver on 9 October 2026.
    RUNNING = ('{"initiatedBy":{"automated":true},"sync":{"revision":"abc"}}', "", 0)

    @pytest.mark.asyncio
    async def test_sends_the_json_patch_that_removes_the_operation(self, connector) -> None:
        """The measured repair: remove /operation, not the finalizer."""
        run = AsyncMock(side_effect=[self.RUNNING, ("patched", "", 0)])
        with patch.object(connector, "_run_kubectl_command", run):
            assert await connector.terminate_argocd_application_operation("mpfm-w3h-pr-310", "argocd") is True

        read, patch_args = (call.args[0] for call in run.await_args_list)
        assert read == ["get", "application", "mpfm-w3h-pr-310", "-n", "argocd", "-o", "jsonpath={.operation}"]
        assert patch_args[:5] == ["patch", "application", "mpfm-w3h-pr-310", "-n", "argocd"]
        assert "--type" in patch_args
        assert patch_args[patch_args.index("--type") + 1] == "json"
        assert patch_args[-1] == '[{"op":"remove","path":"/operation"}]'

    @pytest.mark.asyncio
    async def test_no_operation_running_is_not_patched_at_all(self, connector) -> None:
        """The reason this reads before it patches (#205).

        A json-patch remove on an absent path is rejected, and the apiserver of
        odcn-production rejects it with a generic 422 that names neither the path nor the
        reason: ``the server rejected our request due to an error in our request``. Reading
        the outcome off that message logged an ERROR on every single delete, because an
        Application with no operation on it is the most common case there is.
        """
        run = AsyncMock(return_value=("", "", 0))
        with patch.object(connector, "_run_kubectl_command", run):
            assert await connector.terminate_argocd_application_operation("app-a") is True

        assert [call.args[0][0] for call in run.await_args_list] == ["get"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stderr",
        [
            "The request is invalid: the server rejected our request due to an error in our request",
            'remove operation does not apply: doc is missing path: "/operation": missing value',
            'Error from server (NotFound): applications.argoproj.io "app-a" not found',
        ],
    )
    async def test_an_operation_that_ends_between_the_read_and_the_patch_is_not_a_failure(
        self, connector, stderr: str
    ) -> None:
        """A sync can finish, and the Application can go, in between the two calls.

        Which of the two happened is read, not inferred from the rejection: the generic 422
        of odcn-production carries neither the path nor the reason, so the message cannot
        tell this apart from a patch that genuinely failed.
        """
        gone = ("", "", 0)
        with patch.object(
            connector, "_run_kubectl_command", AsyncMock(side_effect=[self.RUNNING, ("", stderr, 1), gone])
        ):
            assert await connector.terminate_argocd_application_operation("app-a") is True

    @pytest.mark.asyncio
    async def test_missing_application_is_not_a_failure(self, connector) -> None:
        stderr = 'Error from server (NotFound): applications.argoproj.io "app-a" not found'
        run = AsyncMock(return_value=("", stderr, 1))
        with patch.object(connector, "_run_kubectl_command", run):
            assert await connector.terminate_argocd_application_operation("app-a") is True

        assert [call.args[0][0] for call in run.await_args_list] == ["get"]

    @pytest.mark.asyncio
    async def test_an_unreadable_application_is_a_failure(self, connector) -> None:
        """Not knowing whether an operation is running is not the same as there being none:
        the block this clears is what keeps a delete hanging for a fortnight.
        """
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", "connection reset", 1))):
            assert await connector.terminate_argocd_application_operation("app-a") is False

    @pytest.mark.asyncio
    async def test_a_patch_that_fails_with_the_operation_still_there_is_a_failure(self, connector) -> None:
        with patch.object(
            connector,
            "_run_kubectl_command",
            AsyncMock(side_effect=[self.RUNNING, ("", "the request is invalid", 1), self.RUNNING]),
        ):
            assert await connector.terminate_argocd_application_operation("app-a") is False

    @pytest.mark.asyncio
    async def test_a_patch_whose_outcome_cannot_be_read_back_is_a_failure(self, connector) -> None:
        """The re-read is there to tell a race apart from a real failure, so a re-read that
        itself fails may not stand in for 'the operation is gone'."""
        with patch.object(
            connector,
            "_run_kubectl_command",
            AsyncMock(side_effect=[self.RUNNING, ("", "the request is invalid", 1), ("", "connection reset", 1)]),
        ):
            assert await connector.terminate_argocd_application_operation("app-a") is False


class TestTerminateOperationWithoutLogNoise:
    """RC-238: elke delete opende met een loze ERROR.

    De read-voor-de-patch zelf staat in TestTerminateOperation hierboven (RC-250). Wat deze
    toetsen eraan vastzetten is de bevinding waar RC-238 om begon: dat er geen ERROR meer
    in het log komt. De afwijzing van de patch is niet aan haar tekst te herkennen, want
    kubectl tegen k8s 1.32 schrijft alleen "The request is invalid: the server rejected our
    request due to an error in our request", zonder het pad en zonder de reden.
    """

    APISERVER_REJECTION = "The request is invalid: the server rejected our request due to an error in our request"

    @pytest.mark.asyncio
    async def test_an_application_without_an_operation_is_not_patched_and_logs_no_error(
        self, connector, caplog
    ) -> None:
        """De kern van de bevinding: geen patch, dus ook geen ERROR."""
        run = AsyncMock(side_effect=[("", "", 0)])
        with patch.object(connector, "_run_kubectl_command", run), caplog.at_level(logging.DEBUG):
            assert await connector.terminate_argocd_application_operation("nd-j7s-pr-63", "argocd") is True

        assert run.await_count == 1, "een lege operation hoort niet gepatcht te worden"
        assert run.await_args_list[0].args[0][0] == "get"
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []

    @pytest.mark.asyncio
    async def test_the_contentless_apiserver_rejection_is_benign_when_the_operation_is_gone(
        self, connector, caplog
    ) -> None:
        """De operation verdween tussen de read en de patch: dat is de gewenste uitkomst."""
        run = AsyncMock(side_effect=[('{"sync":{}}', "", 0), ("", self.APISERVER_REJECTION, 1), ("", "", 0)])
        with patch.object(connector, "_run_kubectl_command", run), caplog.at_level(logging.DEBUG):
            assert await connector.terminate_argocd_application_operation("nd-j7s-pr-63", "argocd") is True

        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []

    @pytest.mark.asyncio
    async def test_the_contentless_apiserver_rejection_stays_an_error_while_the_operation_remains(
        self, connector, caplog
    ) -> None:
        """Dezelfde tekst, andere staat. Op de tekst matchen zou dit groen maken."""
        run = AsyncMock(side_effect=[('{"sync":{}}', "", 0), ("", self.APISERVER_REJECTION, 1), ('{"sync":{}}', "", 0)])
        with patch.object(connector, "_run_kubectl_command", run), caplog.at_level(logging.DEBUG):
            assert await connector.terminate_argocd_application_operation("nd-j7s-pr-63", "argocd") is False

        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] != []


class TestDeletionProgress:
    """RC-238: traag en vastgelopen zijn alleen te scheiden door te kijken of er iets beweegt.

    Wat beweegt is de INVENTARIS van de doelnamespace, niet de ``status.resources`` van de
    Application zelf. Gemeten op de sandbox op 1 oktober 2026: die blijft tijdens de hele
    cascade op zijn oude getal staan (zie de docstring van de connector), en een bevroren
    getal leest als stilstand.
    """

    @staticmethod
    def _application(deleting: bool, destination: str = "rig-nd-j7s") -> str:
        metadata: dict = {"name": "nd-j7s-pr-63"}
        if deleting:
            metadata["deletionTimestamp"] = "2026-09-29T14:37:02Z"
        return json.dumps(
            {
                "metadata": metadata,
                "spec": {"destination": {"namespace": destination}},
                # Bevroren op 30 terwijl de cascade loopt: dit veld mag de meting niet zijn.
                "status": {"resources": [{"kind": "ConfigMap"} for _ in range(30)]},
            }
        )

    @pytest.mark.asyncio
    async def test_a_deleting_application_counts_the_resources_left_in_its_namespace(self, connector) -> None:
        tracked = [_tracked("nd-j7s-pr-63", f"cm-{i}") for i in range(3)]
        with (
            patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(self._application(True), "", 0))),
            patch.object(connector, "list_tracked_resources", AsyncMock(return_value=(tracked, True))) as inventaris,
        ):
            assert await connector.argocd_application_deletion_progress("nd-j7s-pr-63", "argocd") == (True, 3, True)

        assert inventaris.await_args.args[0] == "rig-nd-j7s", "de inventaris hoort in de DOELnamespace te kijken"

    @pytest.mark.asyncio
    async def test_the_frozen_status_resources_of_the_application_is_not_the_measurement(self, connector) -> None:
        """De kern van de meting: 30 in ``status.resources``, 2 echt over.

        Zonder deze toets is de meting weer uit het statusveld te lezen, en dat veld staat
        tijdens een cascade stil: de stilstandteller loopt dan vol en de wacht stopt op
        dezelfde 40 polls als voor deze wijziging.
        """
        tracked = [_tracked("nd-j7s-pr-63", "cm-1"), _tracked("buur-pr-9", "cm-2")]
        with (
            patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(self._application(True), "", 0))),
            patch.object(connector, "list_tracked_resources", AsyncMock(return_value=(tracked, True))),
        ):
            assert await connector.argocd_application_deletion_progress("nd-j7s-pr-63", "argocd") == (True, 2, True)

    @pytest.mark.asyncio
    async def test_an_application_that_is_not_being_deleted_says_so(self, connector) -> None:
        with (
            patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(self._application(False), "", 0))),
            patch.object(connector, "list_tracked_resources", AsyncMock(return_value=([], True))),
        ):
            assert await connector.argocd_application_deletion_progress("nd-j7s-pr-63", "argocd") == (False, 0, True)

    @pytest.mark.asyncio
    async def test_a_partial_inventory_is_reported_as_one(self, connector) -> None:
        """Een halve inventaris is een getal dat niet met een heel getal te vergelijken is,
        dus de volledigheid hoort in het antwoord. Juist tijdens het incident gaf de
        inventaris exit 1 met output, en als dat als None zou gelden verdient de delete nooit
        meer voortgang."""
        tracked = [_tracked("nd-j7s-pr-63", "cm-1")]
        with (
            patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(self._application(True), "", 0))),
            patch.object(connector, "list_tracked_resources", AsyncMock(return_value=(tracked, False))),
        ):
            assert await connector.argocd_application_deletion_progress("nd-j7s-pr-63", "argocd") == (True, 1, False)

    @pytest.mark.asyncio
    async def test_the_discovered_types_are_passed_on_to_the_inventory(self, connector) -> None:
        """De aanroeper doet de discovery een keer per wacht; geeft hij de types mee, dan
        mag de inventaris er geen tweede keer om vragen."""
        with (
            patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(self._application(True), "", 0))),
            patch.object(connector, "list_tracked_resources", AsyncMock(return_value=([], True))) as inventaris,
        ):
            await connector.argocd_application_deletion_progress("nd-j7s-pr-63", "argocd", ["secrets", "pods"])

        assert inventaris.await_args.args[1] == ["secrets", "pods"]

    @pytest.mark.asyncio
    async def test_an_application_without_a_destination_namespace_cannot_be_measured(self, connector) -> None:
        """Zonder doelnamespace is er niets om de cascade in te tellen, en 0 teruggeven zou
        een stilstand verzinnen."""
        payload = json.dumps({"metadata": {"deletionTimestamp": "2026-09-29T14:37:02Z"}, "spec": {}})
        with (
            patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(payload, "", 0))),
            patch.object(connector, "list_tracked_resources", AsyncMock(return_value=([], True))) as inventaris,
        ):
            assert await connector.argocd_application_deletion_progress("nd-j7s-pr-63", "argocd") is None

        inventaris.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_namespace_that_cannot_be_inventoried_is_none_and_not_zero(self, connector) -> None:
        """Dezelfde reden als bij een onleesbare Application: 0 is een meting, None niet."""
        with (
            patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(self._application(True), "", 0))),
            patch.object(connector, "list_tracked_resources", AsyncMock(return_value=None)),
        ):
            assert await connector.argocd_application_deletion_progress("nd-j7s-pr-63", "argocd") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stdout", "stderr", "code"),
        [
            ("", 'Error from server (NotFound): applications.argoproj.io "a" not found', 1),
            ("", "connection reset", 1),
            ("", "", 0),
            ("not json", "", 0),
            ("[1, 2]", "", 0),
        ],
    )
    async def test_an_unreadable_application_is_none_and_not_zero_progress(
        self, connector, stdout: str, stderr: str, code: int
    ) -> None:
        """None is niet "niets beweegt".

        Een read die mislukte zegt niets over de cascade. De wacht mag er geen stilstand van
        maken, en er net zo goed geen extra venster op verdienen.
        """
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(stdout, stderr, code))):
            assert await connector.argocd_application_deletion_progress("a", "argocd") is None


class TestDeleteApplication:
    @pytest.mark.asyncio
    async def test_an_application_that_is_already_gone_is_not_a_failure(self, connector) -> None:
        """The orphan cleanup reports the outcome of this call, so an Application that went
        away between the listing and the delete has to read as deleted and not as failed.
        ``--ignore-not-found=true`` is what makes kubectl exit 0 there.
        """
        run = AsyncMock(return_value=("", "", 0))
        with patch.object(connector, "_run_kubectl_command", run):
            assert await connector.delete_argocd_application("mpfm-w3h-pr-310", "argocd-test") is True

        args = run.await_args.args[0]
        assert args[:5] == ["delete", "application", "mpfm-w3h-pr-310", "-n", "argocd-test"]
        assert "--ignore-not-found=true" in args

    @pytest.mark.asyncio
    async def test_a_refused_delete_is_reported_as_one(self, connector) -> None:
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", "connection reset", 1))):
            assert await connector.delete_argocd_application("app-a", "argocd") is False


class TestDestinationNamespace:
    @pytest.mark.asyncio
    async def test_reads_the_destination_namespace(self, connector) -> None:
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("rig-prd-mpfm-w3h", "", 0))):
            assert await connector.get_argocd_application_destination_namespace("app-a") == "rig-prd-mpfm-w3h"

    @pytest.mark.asyncio
    async def test_empty_answer_is_not_a_namespace(self, connector) -> None:
        """An empty jsonpath result exits 0; reading it as a namespace would sweep nothing."""
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", "", 0))):
            assert await connector.get_argocd_application_destination_namespace("app-a") is None


class TestResourceTypeDiscovery:
    @pytest.mark.asyncio
    async def test_discovers_namespaced_types_and_skips_events(self, connector) -> None:
        stdout = "secrets\nconfigmaps\nevents\ndeployments.apps\nevents.events.k8s.io\n"
        run = AsyncMock(return_value=(stdout, "", 0))
        with patch.object(connector, "_run_kubectl_command", run):
            types = await connector.list_namespaced_resource_types()

        assert types == ["secrets", "configmaps", "deployments.apps"]
        assert run.await_args.args[0] == [
            "api-resources",
            "--namespaced=true",
            "--verbs=list,delete",
            "-o",
            "name",
        ]

    @pytest.mark.asyncio
    async def test_skips_what_argocd_excludes_itself(self, connector) -> None:
        """Endpoints copy their Service's labels and carry no ownerReference, so an
        operator's Endpoints read as an orphan. ArgoCD excludes the kind, so it is never one."""
        stdout = "secrets\nendpoints\nendpointslices.discovery.k8s.io\n"
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(stdout, "", 0))):
            assert await connector.list_namespaced_resource_types() == ["secrets"]

    @pytest.mark.asyncio
    async def test_a_failed_discovery_is_not_an_empty_cluster(self, connector) -> None:
        """Reading this as 'no types' makes every namespace inventory as empty (RC-226)."""
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", "boom", 1))):
            assert await connector.list_namespaced_resource_types() is None


class TestKindDiscovery:
    #: Shape of ``api-resources -o wide --no-headers`` as measured on odcn-production,
    #: 9 October 2026: ``routes`` has no SHORTNAMES and does have CATEGORIES.
    WIDE = (
        "configmaps        cm     v1                     true   ConfigMap              create,delete,get,list\n"
        "ingresses         ing    networking.k8s.io/v1   true   Ingress                create,delete,get,list\n"
        "routes                   route.openshift.io/v1  true   Route                  create,delete,get,list   all\n"
    )

    @pytest.mark.asyncio
    async def test_maps_kind_and_group_onto_the_type_name(self, connector) -> None:
        run = AsyncMock(return_value=(self.WIDE, "", 0))
        with patch.object(connector, "_run_kubectl_command", run):
            kinds = await connector.list_namespaced_kinds()

        assert kinds == {
            "ConfigMap": "configmaps",
            "Ingress.networking.k8s.io": "ingresses.networking.k8s.io",
            "Route.route.openshift.io": "routes.route.openshift.io",
        }

    @pytest.mark.asyncio
    async def test_a_failed_discovery_is_not_an_empty_cluster(self, connector) -> None:
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", "boom", 1))):
            assert await connector.list_namespaced_kinds() is None


class TestListArgocdApplications:
    """The sweep decides from this list what to delete, so it must read the honest source."""

    @pytest.mark.asyncio
    async def test_reads_the_application_crs_through_kubectl(self, connector) -> None:
        import json

        payload = {"items": [{"metadata": {"name": "mpfm-w3h-pr-310"}, "spec": {"source": {"path": "a/b/c"}}}]}
        run = AsyncMock(return_value=(json.dumps(payload), "", 0))
        with patch.object(connector, "_run_kubectl_command", run):
            applications = await connector.list_argocd_applications("argocd")

        assert [app["metadata"]["name"] for app in applications] == ["mpfm-w3h-pr-310"]
        assert run.await_args.args[0] == ["get", "applications", "-n", "argocd", "-o", "json"]

    @pytest.mark.asyncio
    async def test_a_failed_query_yields_no_applications(self, connector) -> None:
        """Empty must never be read as 'nothing exists': that would orphan every resource."""
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", "timeout", 1))):
            assert await connector.list_argocd_applications() == []


class TestListTrackedResources:
    @pytest.mark.asyncio
    async def test_keeps_only_the_resources_argocd_tracks(self, connector) -> None:
        payload = {
            "items": [
                _item("Secret", "v1", "db-creds", "app-a:/Secret:ns/db-creds"),
                _item("Secret", "v1", "sops-age-key", None),
                _item("Deployment", "apps/v1", "web", "app-b:apps/Deployment:ns/web"),
            ]
        }
        import json

        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(json.dumps(payload), "", 0))):
            tracked, complete = await connector.list_tracked_resources(
                "rig-prd-mpfm-w3h", ["secrets", "deployments.apps"]
            )

        assert [(r.name, r.app_name) for r in tracked] == [("db-creds", "app-a"), ("web", "app-b")]
        assert complete is True

    @pytest.mark.asyncio
    async def test_partial_output_is_kept_and_reported_as_partial(self, connector) -> None:
        """kubectl exits non-zero when ONE queried type fails, but prints the rest.

        Dropping that would report an empty namespace while resources are still standing.
        Handing it over as a WHOLE answer is the other half of the same failure, and the
        one that got through review: the force then deletes the part that was printed,
        reports ``success`` with no errors, and takes the finalizer off, while the types
        that stayed silent keep their resources. Measured with ``complete`` pinned to
        ``True``: 0 red here and green all the way out to the force and the sweep.
        """
        import json

        payload = {"items": [_item("Secret", "v1", "db-creds", "app-a:/Secret:ns/db-creds")]}
        stderr = "error: unable to retrieve the complete list of server APIs: metrics.k8s.io/v1beta1"
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(json.dumps(payload), stderr, 1))):
            tracked, complete = await connector.list_tracked_resources("rig-prd-mpfm-w3h", ["secrets"])

        assert [r.name for r in tracked] == ["db-creds"]
        assert complete is False

    @pytest.mark.asyncio
    async def test_without_types_nothing_was_read_and_nothing_is_claimed(self, connector) -> None:
        """Querying no types answers nothing about the namespace, so it is not 'empty'."""
        run = AsyncMock()
        with patch.object(connector, "_run_kubectl_command", run):
            assert await connector.list_tracked_resources("ns", []) is None
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_discovery_stops_the_inventory(self, connector) -> None:
        """The default path discovers its own types; that failing must not read as empty."""
        run = AsyncMock()
        with (
            patch.object(connector, "list_namespaced_resource_types", AsyncMock(return_value=None)),
            patch.object(connector, "_run_kubectl_command", run),
        ):
            assert await connector.list_tracked_resources("ns") is None
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_query_is_not_an_empty_namespace(self, connector) -> None:
        """kubectl non-zero with no output: a timeout after 120s looks exactly like this."""
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", "timed out", 1))):
            assert await connector.list_tracked_resources("ns", ["secrets"]) is None

    @pytest.mark.asyncio
    async def test_an_empty_answer_from_a_successful_query_is_an_empty_namespace(self, connector) -> None:
        """The other side of the same coin: exit 0 and nothing found really is nothing."""
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", "", 0))):
            assert await connector.list_tracked_resources("ns", ["secrets"]) == ([], True)

    @pytest.mark.asyncio
    async def test_unparsable_output_is_not_read_as_empty_silently(self, connector) -> None:
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("not json", "", 0))):
            assert await connector.list_tracked_resources("ns", ["secrets"]) is None


class TestTypesNamedInKubectlError:
    """De naamherkenning los, want via ``list_tracked_resources`` is maar een vorm per
    toets te meten en de randen zitten juist in de tekst."""

    FORBIDDEN = 'Error from server (Forbidden): {t} is forbidden: User "opi" cannot list resource'

    @pytest.mark.parametrize(
        ("stderr", "types", "verwacht"),
        [
            ("", ["secrets"], []),
            ("Error from server (Forbidden): secrets is forbidden", [], []),
            ("Error from server (Forbidden): secrets is forbidden", ["secrets"], ["secrets"]),
            # Twee missers in een antwoord: beide horen in het log, in de volgorde van de query.
            (
                "Error from server (Forbidden): secrets is forbidden\n"
                "Error from server (Forbidden): configmaps is forbidden",
                ["configmaps", "secrets", "endpoints"],
                ["configmaps", "secrets"],
            ),
            # De naamgrens, beide kanten op.
            ("cannot list resource externalsecrets.external-secrets.io", ["secrets"], []),
            ('cannot list resource "secrets" in API group ""', ["secrets"], ["secrets"]),
            ("deployments.apps is forbidden", ["deployments"], []),
            # Een waarschuwing noemt een Kind met hoofdletter, nooit de plural.group die
            # wordt opgevraagd. Zou de match ongevoelig voor kapitalen worden, dan wijst
            # het log een type aan dat gewoon gelezen is.
            (
                "Warning: v1 Endpoints is deprecated in v1.33+\n"
                "Error from server (Forbidden): deployments.apps is forbidden",
                ["endpoints", "deployments.apps"],
                ["deployments.apps"],
            ),
        ],
    )
    def test_which_types_kubectl_blamed(self, stderr: str, types: list[str], verwacht: list[str]) -> None:
        assert types_named_in_kubectl_error(stderr, types) == verwacht

    def test_a_type_with_a_regex_character_is_matched_literally(self) -> None:
        """``.`` in ``deployments.apps`` is een punt en geen joker."""
        assert types_named_in_kubectl_error(self.FORBIDDEN.format(t="deploymentsxapps"), ["deployments.apps"]) == []


class TestPartialInventoryNamesTheFailingType:
    """RC-238: tijdens het incident gaf de inventaris exit 1 en stond er alleen de ruwe
    stderr in het log. Welk van de 50 opgevraagde types niet gelezen kon worden was
    daarmee niet te zien, en juist die types zijn waar een achtergebleven resource zit.

    De vormen hieronder zijn op de sandbox nagemeten (k8s 1.32): kubectl noemt het type
    precies zoals het is opgevraagd.
    """

    FORBIDDEN_GROUPED = (
        'Error from server (Forbidden): deployments.apps is forbidden: User "opi" cannot '
        'list resource "deployments" in API group "apps" in the namespace "rig-prd-nd-j7s"'
    )
    DEPRECATION_WARNING = "Warning: v1 Endpoints is deprecated in v1.33+"

    @staticmethod
    def _payload() -> str:
        return json.dumps({"items": [_item("Secret", "v1", "db-creds", "app-a:/Secret:ns/db-creds")]})

    @pytest.mark.asyncio
    async def test_exactly_one_failing_type_is_named_in_the_log(self, connector, caplog) -> None:
        """Precies een van de opgevraagde types faalt: complete=False en het log noemt hem."""
        types = ["secrets", "configmaps", "deployments.apps"]
        with (
            patch.object(
                connector, "_run_kubectl_command", AsyncMock(return_value=(self._payload(), self.FORBIDDEN_GROUPED, 1))
            ),
            caplog.at_level(logging.DEBUG),
        ):
            tracked, complete = await connector.list_tracked_resources("rig-prd-nd-j7s", types)

        assert [r.name for r in tracked] == ["db-creds"]
        assert complete is False
        gemeld = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        # Op de naam EN op de telling, want de ruwe stderr eronder bevat "deployments.apps"
        # ook: zonder dit stuk is de oude regel die alleen stderr doorgaf al groen.
        assert any("1 of 3 types could not be listed (deployments.apps)" in m for m in gemeld), gemeld

    @pytest.mark.asyncio
    async def test_a_type_that_merely_contains_another_name_is_not_blamed(self, connector, caplog) -> None:
        """``secrets`` zit in ``externalsecrets.external-secrets.io``.

        Zonder naamgrens wijst het log de verkeerde type-naam aan, en dat stuurt de lezer
        naar de verkeerde plek.
        """
        stderr = (
            "Error from server (Forbidden): externalsecrets.external-secrets.io is forbidden: "
            'User "opi" cannot list resource "externalsecrets" in API group "external-secrets.io" '
            'in the namespace "rig-prd-nd-j7s"'
        )
        types = ["secrets", "externalsecrets.external-secrets.io"]
        with (
            patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(self._payload(), stderr, 1))),
            caplog.at_level(logging.DEBUG),
        ):
            await connector.list_tracked_resources("rig-prd-nd-j7s", types)

        assert types_named_in_kubectl_error(stderr, types) == ["externalsecrets.external-secrets.io"]

    @pytest.mark.asyncio
    async def test_a_deprecation_warning_on_a_successful_query_keeps_the_inventory_complete(
        self, connector, caplog
    ) -> None:
        """Een waarschuwing op stderr met exit 0 is geen misser.

        Dit deed hij al; de toets staat er zodat de diagnostiek-route hem niet stuk maakt.
        """
        with (
            patch.object(
                connector,
                "_run_kubectl_command",
                AsyncMock(return_value=(self._payload(), self.DEPRECATION_WARNING, 0)),
            ),
            caplog.at_level(logging.DEBUG),
        ):
            tracked, complete = await connector.list_tracked_resources("rig-prd-nd-j7s", ["secrets", "endpoints"])

        assert complete is True
        assert [r.name for r in tracked] == ["db-creds"]

    @pytest.mark.asyncio
    async def test_an_unrecognisable_failure_says_so_instead_of_blaming_a_type(self, connector, caplog) -> None:
        """Noemt kubectl geen enkel opgevraagd type, dan is dat wat het log zegt.

        Een lege lijst mag niet als "niets faalde" lezen, want ``complete`` is wel False.
        """
        stderr = "error: unable to retrieve the complete list of server APIs: metrics.k8s.io/v1beta1"
        with (
            patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(self._payload(), stderr, 1))),
            caplog.at_level(logging.DEBUG),
        ):
            _, complete = await connector.list_tracked_resources("rig-prd-nd-j7s", ["secrets", "configmaps"])

        assert complete is False
        gemeld = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("named none of the 2 queried types" in m for m in gemeld), gemeld


class TestDeleteTrackedResources:
    @staticmethod
    def _resource(name: str) -> TrackedResource:
        return TrackedResource(
            kind="Secret",
            api_version="v1",
            name=name,
            namespace="rig-prd-mpfm-w3h",
            app_name="app-a",
            from_label=False,
        )

    @pytest.mark.asyncio
    async def test_deletes_each_resource_by_its_own_type(self, connector) -> None:
        delete = AsyncMock(return_value=True)
        with patch.object(connector, "delete_resource", delete):
            failed = await connector.delete_tracked_resources([self._resource("a"), self._resource("b")])

        assert failed == []
        assert [call.args for call in delete.await_args_list] == [
            ("secret", "a", "rig-prd-mpfm-w3h"),
            ("secret", "b", "rig-prd-mpfm-w3h"),
        ]

    @pytest.mark.asyncio
    async def test_reports_the_ones_that_would_not_go(self, connector) -> None:
        with patch.object(connector, "delete_resource", AsyncMock(side_effect=[True, False])):
            failed = await connector.delete_tracked_resources([self._resource("a"), self._resource("b")])

        assert [r.name for r in failed] == ["b"]


# ---------------------------------------------------------------------------
# The delete route: the order is the fix
# ---------------------------------------------------------------------------


def _recording_kubectl(
    calls: list[str],
    *,
    destination: str | None,
    tracked: list[TrackedResource] | None,
    complete: bool = True,
    deleted: bool = True,
    applications: list[str] | None = None,
) -> AsyncMock:
    """A kubectl stand-in that writes the name of each call into ``calls``.

    ``spec_set=KubectlConnector`` for the same reason the ArgoConnector mock below carries a
    spec, and ``spec_set`` rather than ``spec`` because here it is the stubbing that has to
    fail, which only ``spec_set`` restricts.

    ``applications`` are the Application names the cluster answers with; the default is the
    one every platform cluster runs, so it is a read that SUCCEEDED and found no neighbour.
    An empty list is how the connector answers a failed read.

    ``complete`` is the second half of the inventory answer: False is a namespace that
    answered for only part of its resource types.
    """

    def step(name: str, result):
        async def _run(*args, **kwargs):
            calls.append(name)
            return result

        return AsyncMock(side_effect=_run)

    kubectl = AsyncMock(spec_set=KubectlConnector)
    kubectl.terminate_argocd_application_operation = step("terminate_operation", True)
    kubectl.get_argocd_application_destination_namespace = step("read_namespace", destination)
    kubectl.list_tracked_resources = step("list_resources", None if tracked is None else (tracked, complete))
    kubectl.delete_tracked_resources = step("delete_resources", [])
    kubectl.remove_argocd_application_finalizers = step("remove_finalizers", True)
    kubectl.delete_argocd_application = step("delete_application", deleted)
    kubectl.list_argocd_applications = step(
        "list_applications",
        [{"metadata": {"name": name}} for name in (["user-applications"] if applications is None else applications)],
    )
    kubectl.delete_namespace = AsyncMock(return_value=True)
    kubectl._run_kubectl_command = AsyncMock(return_value=("", "", 0))
    return kubectl


def _orphan_cleanup_harness(calls: list[str], *, deleted: bool = True) -> tuple[AsyncMock, MagicMock]:
    """A project manager and an ArgoConnector for the orphan-cleanup route.

    ``spec=ArgoConnector`` is the point of the mock here: an open AsyncMock invents any
    method you name, and that is how a call to a method the connector does not have
    stayed green while it did nothing (the AttributeError landed in the ``except Exception``
    of ``_cleanup_orphaned_argocd_resources``).
    """
    pm = AsyncMock()
    pm._kubectl_connector = _recording_kubectl(calls, destination="ns", tracked=[], deleted=deleted)
    argo = MagicMock(spec=ArgoConnector)
    argo.list_applications = AsyncMock(return_value=[{"metadata": {"name": "mpfm-w3h-pr-310"}}])
    return pm, argo


def _targets(pm: AsyncMock, method: str) -> list[str]:
    """The Application name each call to ``method`` was aimed at.

    Order alone does not pin this: clearing the operation of some OTHER Application runs
    at exactly the right moment and changes nothing, so the block that RC-226 is about
    stays in place while every sequence assertion here still passes.
    """
    return [call.args[0] for call in getattr(pm._kubectl_connector, method).await_args_list]


def _tracked(app_name: str, name: str, from_label: bool = False) -> TrackedResource:
    return TrackedResource(
        kind="Secret",
        api_version="v1",
        name=name,
        namespace="rig-prd-mpfm-w3h",
        app_name=app_name,
        from_label=from_label,
    )


class TestTerminationIsReported:
    """The caller has to be able to see that the operation could NOT be cleared.

    A delete that reports success while the block is still in place is the state this
    task is about: it looks done and it is not.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("terminated", "status"), [(True, "success"), (False, "failed")])
    async def test_the_outcome_lands_in_the_deletion_results(self, terminated: bool, status: str) -> None:
        pm = AsyncMock()
        pm._kubectl_connector.terminate_argocd_application_operation = AsyncMock(return_value=terminated)
        results: dict = {"operations": [], "errors": []}

        await DeleteProjectManager(pm)._terminate_application_operation("app-a", results)

        operation = next(op for op in results["operations"] if op["type"] == "argocd_app_operation_termination")
        assert (operation["target"], operation["status"]) == ("app-a", status)


class TestForceDeleteStuckApplication:
    @pytest.mark.asyncio
    async def test_resources_go_before_the_finalizer(self) -> None:
        """The whole point: remove the finalizer first and the cascade never runs."""
        calls: list[str] = []
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(
            calls, destination="rig-prd-mpfm-w3h", tracked=[_tracked("app-a", "db-creds")]
        )
        results: dict = {"operations": [], "errors": []}

        assert await DeleteProjectManager(pm)._force_delete_stuck_application("app-a", results) == ForcedDeletion(
            finalizer_removed=True, swept_partially=False
        )

        assert calls == [
            "read_namespace",
            "list_resources",
            "list_applications",
            "delete_resources",
            "remove_finalizers",
        ]

    @pytest.mark.asyncio
    async def test_only_the_resources_of_this_application_are_deleted(self) -> None:
        """A namespace holds several deployments; a delete may only take its own."""
        calls: list[str] = []
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(
            calls,
            destination="rig-prd-mpfm-w3h",
            tracked=[_tracked("app-a", "mine"), _tracked("app-b", "someone-elses")],
        )
        results: dict = {"operations": [], "errors": []}

        await DeleteProjectManager(pm)._force_delete_stuck_application("app-a", results)

        deleted = pm._kubectl_connector.delete_tracked_resources.await_args.args[0]
        assert [r.name for r in deleted] == ["mine"]

    #: 63 characters, so a whole Application name AND what a longer one is cut to. The schema
    #: allows 30 for the project plus 63 for the deployment, and several deployments share a
    #: namespace (projects/simple-example.yaml), so this neighbour is an ordinary situation.
    NEIGHBOUR = "p-" + "b" * 61
    FORCED = NEIGHBOUR + "-x"

    @pytest.mark.asyncio
    async def test_a_label_at_the_cap_is_not_taken_for_this_applications_own(self) -> None:
        """Its secrets and PVCs are as likely the neighbour's, and here a match is a delete.

        The sister test above says a delete may only take its own; this is the name length at
        which an exact comparison stops being able to tell, which app-a and app-b never reach.
        """
        assert len(self.NEIGHBOUR) == LABEL_VALUE_MAX
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(
            [],
            destination="rig-prd-mpfm-w3h",
            tracked=[_tracked(self.NEIGHBOUR, "neighbours-secret", from_label=True)],
        )
        results: dict = {"operations": [], "errors": []}

        await DeleteProjectManager(pm)._force_delete_stuck_application(self.FORCED, results)

        assert pm._kubectl_connector.delete_tracked_resources.await_args.args[0] == []

    @pytest.mark.asyncio
    async def test_what_it_could_not_place_is_reported_instead_of_left_in_silence(self) -> None:
        """Leaving them without a word is the silent force this task is about, through the
        other door: not a failed inventory, but a successful one it cannot act on."""
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(
            [],
            destination="rig-prd-mpfm-w3h",
            tracked=[_tracked(self.NEIGHBOUR, "neighbours-secret", from_label=True)],
        )
        results: dict = {"operations": [], "errors": []}

        assert await DeleteProjectManager(pm)._force_delete_stuck_application(self.FORCED, results) == ForcedDeletion(
            finalizer_removed=False, swept_partially=True
        )

        operation = next(op for op in results["operations"] if op["type"] == "argocd_app_tracked_resource_deletion")
        assert (operation["undecidable"], operation["deleted"], operation["status"]) == (1, 0, "partial")
        assert any("left standing" in error for error in results["errors"])
        # A resource it could not place may be this application's own, so the cascade that
        # would still take it stays: the third way into partial, and its own measurement.
        pm._kubectl_connector.remove_argocd_application_finalizers.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_label_equal_to_the_forced_name_is_not_taken_from_a_living_neighbour(self) -> None:
        """The same doubt from the other side, and this is the side that deletes.

        Here the 63-character name is the one being FORCED and the longer one is the
        neighbour that is still running: on a label cluster every resource of that neighbour
        carries exactly the forced name, so an exact comparison hands over its PVC, and a PVC
        does not come back.
        """
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(
            [],
            destination="rig-prd-mpfm-w3h",
            tracked=[_tracked(self.NEIGHBOUR, "neighbours-pvc", from_label=True)],
            applications=[self.NEIGHBOUR, self.FORCED],
        )
        results: dict = {"operations": [], "errors": []}

        await DeleteProjectManager(pm)._force_delete_stuck_application(self.NEIGHBOUR, results)

        assert pm._kubectl_connector.delete_tracked_resources.await_args.args[0] == []
        operation = next(op for op in results["operations"] if op["type"] == "argocd_app_tracked_resource_deletion")
        assert (operation["undecidable"], operation["deleted"], operation["status"]) == (1, 0, "partial")
        assert any("not decidable" in error for error in results["errors"])
        # The names WERE read here. Fired unconditionally, this one sends the reader after a
        # cluster fault, while the error above says the resource itself is what decides.
        assert not any("Could not read the ArgoCD Application names" in error for error in results["errors"])

    @pytest.mark.asyncio
    async def test_without_a_longer_neighbour_a_label_at_the_cap_is_this_applications_own(self) -> None:
        """The counterweight to the test above: 63 characters is an ordinary name too. Calling
        it undecidable by itself leaves a force on a label cluster deleting nothing at all."""
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(
            [],
            destination="rig-prd-mpfm-w3h",
            tracked=[_tracked(self.NEIGHBOUR, "db-creds", from_label=True)],
            applications=[self.NEIGHBOUR],
        )
        results: dict = {"operations": [], "errors": []}

        await DeleteProjectManager(pm)._force_delete_stuck_application(self.NEIGHBOUR, results)

        deleted = pm._kubectl_connector.delete_tracked_resources.await_args.args[0]
        assert [r.name for r in deleted] == ["db-creds"]

    @pytest.mark.asyncio
    async def test_an_unreadable_application_list_leaves_every_label_at_the_cap_standing(self) -> None:
        """The connector answers a failed list with an empty one, and the Application being
        forced is itself still standing, so an empty answer is a failed read. Taking it for
        'there are no neighbours' is precisely the comparison that takes a neighbour's PVC.

        What does not depend on the names still goes: a mark from the annotation knows no cap.
        """
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(
            [],
            destination="rig-prd-mpfm-w3h",
            tracked=[
                _tracked(self.NEIGHBOUR, "maybe-the-neighbours", from_label=True),
                _tracked(self.NEIGHBOUR, "annotated"),
            ],
            applications=[],
        )
        results: dict = {"operations": [], "errors": []}

        await DeleteProjectManager(pm)._force_delete_stuck_application(self.NEIGHBOUR, results)

        deleted = pm._kubectl_connector.delete_tracked_resources.await_args.args[0]
        assert [r.name for r in deleted] == ["annotated"]
        operation = next(op for op in results["operations"] if op["type"] == "argocd_app_tracked_resource_deletion")
        assert operation["undecidable"] == 1
        assert any("Could not read the ArgoCD Application names" in error for error in results["errors"])

    @pytest.mark.asyncio
    async def test_a_long_name_in_the_annotation_is_taken(self) -> None:
        """The annotation has no cap, so on odcn-production a long name compares equal and
        nothing is undecidable. Without this the force reports ``deleted: 0`` on exactly the
        cluster type this task is about.
        """
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(
            [],
            destination="rig-prd-mpfm-w3h",
            tracked=[_tracked(self.FORCED, "db-creds"), _tracked(self.NEIGHBOUR, "neighbours-secret")],
        )
        results: dict = {"operations": [], "errors": []}

        await DeleteProjectManager(pm)._force_delete_stuck_application(self.FORCED, results)

        deleted = pm._kubectl_connector.delete_tracked_resources.await_args.args[0]
        assert [r.name for r in deleted] == ["db-creds"]

    @pytest.mark.asyncio
    async def test_reports_how_much_it_deleted(self) -> None:
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(
            [], destination="rig-prd-mpfm-w3h", tracked=[_tracked("app-a", "a"), _tracked("app-a", "b")]
        )
        results: dict = {"operations": [], "errors": []}

        assert await DeleteProjectManager(pm)._force_delete_stuck_application("app-a", results) == ForcedDeletion(
            finalizer_removed=True, swept_partially=False
        )

        operation = next(op for op in results["operations"] if op["type"] == "argocd_app_tracked_resource_deletion")
        assert operation["status"] == "success"
        assert operation["deleted"] == 2
        assert operation["namespace"] == "rig-prd-mpfm-w3h"
        # The counterweight to the three refusals: gating the finalizer on the sweep may not
        # turn the force itself into a no-op. Refusing always reads as green without this.
        pm._kubectl_connector.remove_argocd_application_finalizers.assert_awaited_once_with("app-a")
        removal = next(op for op in results["operations"] if op["type"] == "argocd_app_finalizer_removal")
        assert removal["status"] == "success"

    @pytest.mark.asyncio
    async def test_resources_that_would_not_go_become_an_error(self) -> None:
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl([], destination="ns", tracked=[_tracked("app-a", "a")])
        pm._kubectl_connector.delete_tracked_resources = AsyncMock(return_value=[_tracked("app-a", "a")])
        results: dict = {"operations": [], "errors": []}

        assert await DeleteProjectManager(pm)._force_delete_stuck_application("app-a", results) == ForcedDeletion(
            finalizer_removed=False, swept_partially=True
        )

        operation = next(op for op in results["operations"] if op["type"] == "argocd_app_tracked_resource_deletion")
        assert operation["status"] == "partial"
        assert results["errors"]
        # The inventory was WHOLE here and the deletes failed: gating the finalizer on
        # completeness alone would take it off over resources demonstrably still standing.
        pm._kubectl_connector.remove_argocd_application_finalizers.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unreadable_namespace_is_reported_and_does_not_stop_the_force(self) -> None:
        """Leaving the app stuck blocks the parent for everyone, so the force still runs.

        What must not happen is that it runs SILENTLY: without a namespace, nothing was
        swept, and that has to reach the caller as an error.
        """
        calls: list[str] = []
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(calls, destination=None, tracked=[])
        results: dict = {"operations": [], "errors": []}

        assert await DeleteProjectManager(pm)._force_delete_stuck_application("app-a", results) == ForcedDeletion(
            finalizer_removed=True, swept_partially=False
        )

        assert calls == ["read_namespace", "remove_finalizers"]
        assert any("destination namespace" in error for error in results["errors"])
        operation = next(op for op in results["operations"] if op["type"] == "argocd_app_tracked_resource_deletion")
        assert operation["status"] == "unknown_namespace"

    @pytest.mark.asyncio
    async def test_a_failed_inventory_is_an_error_and_never_reports_a_clean_sweep(self) -> None:
        """The same branch as an unreadable namespace: force, but say that nothing was swept.

        Reading a failed inventory as 'no resources' is the silent force the docstring of
        this function forbids: deleted 0, no error, finalizer gone, resources standing.
        """
        calls: list[str] = []
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(calls, destination="rig-prd-mpfm-w3h", tracked=None)
        results: dict = {"operations": [], "errors": []}

        assert await DeleteProjectManager(pm)._force_delete_stuck_application("app-a", results) == ForcedDeletion(
            finalizer_removed=True, swept_partially=False
        )

        assert calls == ["read_namespace", "list_resources", "remove_finalizers"]
        pm._kubectl_connector.delete_tracked_resources.assert_not_awaited()
        operation = next(op for op in results["operations"] if op["type"] == "argocd_app_tracked_resource_deletion")
        assert operation["status"] == "inventory_failed"
        assert any("Could not inventory namespace" in error for error in results["errors"])

    @pytest.mark.asyncio
    async def test_a_half_inventory_is_deleted_but_never_reported_as_a_clean_sweep(self) -> None:
        """The third answer: the namespace answered for SOME of its types.

        ``kubectl get a,b,c`` exits non-zero when one type fails and prints the rest, so
        the part that came back is worth deleting, but the part that did not is where the
        leftovers stand. Measured with the connector handing this over as complete: status
        ``success``, ``failed: 0``, ``undecidable: 0``, ``errors: []`` and the finalizer
        gone, which is the exact shape that left 350 resources in rig-prd-mpfm-w3h.

        What the CALLER gets is the other half of that, and it is pinned here rather than
        only on the deletion results: a half sweep answers ``False``, so the Application is
        left standing with the cascade that can still take the rest.
        """
        calls: list[str] = []
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(
            calls, destination="rig-prd-mpfm-w3h", tracked=[_tracked("app-a", "db-creds")], complete=False
        )
        results: dict = {"operations": [], "errors": []}

        assert await DeleteProjectManager(pm)._force_delete_stuck_application("app-a", results) == ForcedDeletion(
            finalizer_removed=False, swept_partially=True
        )

        # The part that DID answer still goes: half a sweep beats none.
        assert [r.name for r in pm._kubectl_connector.delete_tracked_resources.await_args_list[0].args[0]] == [
            "db-creds"
        ]
        assert calls == ["read_namespace", "list_resources", "list_applications", "delete_resources"]
        pm._kubectl_connector.remove_argocd_application_finalizers.assert_not_awaited()
        operation = next(op for op in results["operations"] if op["type"] == "argocd_app_tracked_resource_deletion")
        assert (operation["status"], operation["deleted"]) == ("partial", 1)
        assert any("only part of its resource types" in error for error in results["errors"])
        removal = next(op for op in results["operations"] if op["type"] == "argocd_app_finalizer_removal")
        assert removal["status"] == "skipped_partial_sweep"
        assert any("left standing with its finalizer" in error for error in results["errors"])


def _deployment_harness(
    calls: list[str],
    *,
    wait_results: list[bool] | None = None,
    tracked: list[TrackedResource] | None = None,
    complete: bool = True,
) -> tuple[AsyncMock, AsyncMock, dict, dict]:
    """A deployment delete with a live Application, recording the order of the calls.

    ``wait_results`` feeds successive answers to the deletion wait, so a caller can make
    the first wait time out and reach the force branch.
    """
    pm = AsyncMock()
    pm._kubectl_connector = _recording_kubectl(
        calls, destination="rig-mpfm-w3h", tracked=tracked or [], complete=complete
    )
    pm._manifest_generator = MagicMock()
    pm._manifest_generator.create_kustomization_files = MagicMock(return_value=True)
    pm._keycloak_manager.delete_resources_for_deployment = AsyncMock(return_value={"operations": [], "errors": []})
    pm._minio_manager.delete_resources_for_deployment = AsyncMock(return_value={"operations": [], "errors": []})
    pm._ensure_database_manager.return_value.delete_resources_for_deployment = AsyncMock(
        return_value={"operations": [], "errors": []}
    )

    for factory in ("get_git_connector_for_argocd", "get_git_connector_for_deployment"):
        connector = AsyncMock()
        connector.get_working_dir = AsyncMock(return_value="/tmp/gitops")
        setattr(pm, factory, AsyncMock(return_value=connector))

    answers = list(wait_results or [])

    async def _wait(*args, **kwargs):
        calls.append("wait_for_deletion")
        return answers.pop(0) if answers else True

    argo = AsyncMock()
    argo.refresh_application = AsyncMock(return_value=True)
    argo.application_exists = AsyncMock(return_value=True)
    argo.wait_for_application_deletion = AsyncMock(side_effect=_wait)

    deployment = {
        "name": "pr-310",
        "cluster": "local",
        "namespace": "mpfm-w3h",
        "repository": "main-repo",
        "components": [{"reference": "web"}],
    }
    project_data = {
        "name": "mpfm-w3h",
        "services": [],
        "components": [{"name": "web"}],
        "deployments": [deployment],
        "repositories": [{"name": "main-repo", "path": ""}],
    }
    return pm, argo, project_data, deployment


class TestOperationClearedBeforeTheWait:
    @pytest.mark.asyncio
    async def test_infrastructure_delete_clears_the_operation_before_waiting(self) -> None:
        calls: list[str] = []
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(calls, destination="ns", tracked=[])
        pm._manifest_generator = MagicMock()

        gitops = AsyncMock()
        gitops.get_working_dir = AsyncMock(return_value="/tmp/gitops")
        pm.get_git_connector_for_argocd = AsyncMock(return_value=gitops)

        async def _wait(*args, **kwargs):
            calls.append("wait_for_deletion")
            return False

        argo = AsyncMock()
        argo.refresh_application = AsyncMock(return_value=True)
        argo.application_exists = AsyncMock(return_value=True)
        argo.wait_for_application_deletion = AsyncMock(side_effect=_wait)

        results: dict = {"operations": [], "errors": []}
        project_data = {"name": "mpfm-w3h", "services": ["namespace-postgresql-database"]}

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("os.path.exists", return_value=False),
        ):
            await DeleteProjectManager(pm)._cleanup_project_infrastructure(
                "mpfm-w3h", "local", project_data, results, force=True
            )

        # The operation goes first, then the wait; on the timeout the resources go
        # before the finalizer, and only then does it wait for the app to disappear.
        assert calls == [
            "terminate_operation",
            "wait_for_deletion",
            "read_namespace",
            "list_resources",
            "list_applications",
            "delete_resources",
            "remove_finalizers",
            "wait_for_deletion",
        ]
        assert _targets(pm, "terminate_argocd_application_operation") == ["mpfm-w3h-infrastructure"]
        assert _targets(pm, "get_argocd_application_destination_namespace") == ["mpfm-w3h-infrastructure"]

    @pytest.mark.asyncio
    async def test_the_infrastructure_delete_only_forces_when_asked_to(self) -> None:
        """Without force a timeout stays a timeout: forcing is what leaves rubbish behind,
        so it may not run on an ordinary delete. Its sibling on the deployment route has
        the same gate, and dropping either one on its own goes unnoticed by the other."""
        calls: list[str] = []
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(calls, destination="ns", tracked=[])
        pm._manifest_generator = MagicMock()

        gitops = AsyncMock()
        gitops.get_working_dir = AsyncMock(return_value="/tmp/gitops")
        pm.get_git_connector_for_argocd = AsyncMock(return_value=gitops)

        argo = AsyncMock()
        argo.refresh_application = AsyncMock(return_value=True)
        argo.application_exists = AsyncMock(return_value=True)
        argo.wait_for_application_deletion = AsyncMock(return_value=False)

        results: dict = {"operations": [], "errors": []}
        project_data = {"name": "mpfm-w3h", "services": ["namespace-postgresql-database"]}

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("os.path.exists", return_value=False),
        ):
            await DeleteProjectManager(pm)._cleanup_project_infrastructure(
                "mpfm-w3h", "local", project_data, results, force=False
            )

        assert "remove_finalizers" not in calls
        assert "delete_resources" not in calls

    @pytest.mark.asyncio
    async def test_a_half_swept_infrastructure_application_waits_for_its_cascade(self) -> None:
        """The sibling call site. In a tenant this sweep is partial too, so reverting the
        wait here puts the old outcome back for the infrastructure Application without the
        deployment route noticing.
        """
        calls: list[str] = []
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(
            calls, destination="ns", tracked=[_tracked("mpfm-w3h-infrastructure", "db-creds")], complete=False
        )
        pm._manifest_generator = MagicMock()

        gitops = AsyncMock()
        gitops.get_working_dir = AsyncMock(return_value="/tmp/gitops")
        pm.get_git_connector_for_argocd = AsyncMock(return_value=gitops)

        answers = [False, True]

        async def _wait(*args, **kwargs):
            calls.append("wait_for_deletion")
            return answers.pop(0)

        argo = AsyncMock()
        argo.refresh_application = AsyncMock(return_value=True)
        argo.application_exists = AsyncMock(return_value=True)
        argo.wait_for_application_deletion = AsyncMock(side_effect=_wait)

        results: dict = {"operations": [], "errors": []}
        project_data = {"name": "mpfm-w3h", "services": ["namespace-postgresql-database"]}

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("os.path.exists", return_value=False),
        ):
            await DeleteProjectManager(pm)._cleanup_project_infrastructure(
                "mpfm-w3h", "local", project_data, results, force=True
            )

        assert "remove_finalizers" not in calls
        cascade = next(op for op in results["operations"] if op["type"] == "argocd_app_cascade_wait")
        assert (cascade["target"], cascade["status"]) == ("mpfm-w3h-infrastructure", "success")
        assert results["errors"] == []
        wait = next(op for op in results["operations"] if op["type"] == "infrastructure_app_deletion_wait")
        assert wait["finalizer_removed"] is False

    @pytest.mark.asyncio
    async def test_deleting_a_deployment_clears_the_operation_before_waiting(self) -> None:
        calls: list[str] = []
        pm, argo, project_data, deployment = _deployment_harness(calls)
        pm.get_contents = AsyncMock(return_value=project_data)
        pm.get_deployment_by_name = AsyncMock(return_value=deployment)

        project_store = MagicMock()
        project_store.get = MagicMock(return_value=MagicMock(filename="mpfm-w3h.yaml"))

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("opi.manager.delete_project_manager.get_project_store", return_value=project_store),
            patch("os.path.exists", return_value=False),
        ):
            await DeleteProjectManager(pm).delete_deployment("mpfm-w3h", "pr-310")

        assert calls[:2] == ["terminate_operation", "wait_for_deletion"]
        assert _targets(pm, "terminate_argocd_application_operation") == ["mpfm-w3h-pr-310"]

    @pytest.mark.asyncio
    async def test_a_stuck_deployment_delete_sweeps_its_resources_before_forcing(self) -> None:
        """The force branch of the deployment route, the one the 350 leftovers came out of.

        Its sibling in _cleanup_project_infrastructure runs the same sequence; both call
        sites need pinning, because reverting either one on its own puts the old
        finalizer-first behaviour back without the other noticing.
        """
        calls: list[str] = []
        pm, argo, project_data, deployment = _deployment_harness(
            calls, wait_results=[False, True], tracked=[_tracked("mpfm-w3h-pr-310", "db-creds")]
        )
        pm.get_contents = AsyncMock(return_value=project_data)
        pm.get_deployment_by_name = AsyncMock(return_value=deployment)

        project_store = MagicMock()
        project_store.get = MagicMock(return_value=MagicMock(filename="mpfm-w3h.yaml"))

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("opi.manager.delete_project_manager.get_project_store", return_value=project_store),
            patch("os.path.exists", return_value=False),
        ):
            results = await DeleteProjectManager(pm).delete_deployment("mpfm-w3h", "pr-310", force=True)

        assert calls[:8] == [
            "terminate_operation",
            "wait_for_deletion",
            "read_namespace",
            "list_resources",
            "list_applications",
            "delete_resources",
            "remove_finalizers",
            "wait_for_deletion",
        ]
        deleted = pm._kubectl_connector.delete_tracked_resources.await_args.args[0]
        assert [r.name for r in deleted] == ["db-creds"]
        assert _targets(pm, "get_argocd_application_destination_namespace") == ["mpfm-w3h-pr-310"]
        assert any(op["type"] == "argocd_app_tracked_resource_deletion" for op in results["operations"])

    @pytest.mark.asyncio
    async def test_a_deployment_delete_without_force_does_not_touch_the_finalizer(self) -> None:
        """Without force a timeout stays a timeout: forcing is what leaves rubbish behind."""
        calls: list[str] = []
        pm, argo, project_data, deployment = _deployment_harness(calls, wait_results=[False])
        pm.get_contents = AsyncMock(return_value=project_data)
        pm.get_deployment_by_name = AsyncMock(return_value=deployment)

        project_store = MagicMock()
        project_store.get = MagicMock(return_value=MagicMock(filename="mpfm-w3h.yaml"))

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("opi.manager.delete_project_manager.get_project_store", return_value=project_store),
            patch("os.path.exists", return_value=False),
        ):
            await DeleteProjectManager(pm).delete_deployment("mpfm-w3h", "pr-310")

        assert "remove_finalizers" not in calls
        assert "delete_resources" not in calls

    @pytest.mark.asyncio
    async def test_a_half_swept_deployment_is_reported_as_not_forced_away(self) -> None:
        """What the ROUTE makes of a half sweep, which is what an operator ends up reading.

        The half answer reaches here as ``finalizer_removed: False``, and the finalizer is
        still on the Application: what follows is a wait for the cascade it carries, not a
        second removal.
        """
        calls: list[str] = []
        pm, argo, project_data, deployment = _deployment_harness(
            calls, wait_results=[False, False], tracked=[_tracked("mpfm-w3h-pr-310", "db-creds")], complete=False
        )
        pm.get_contents = AsyncMock(return_value=project_data)
        pm.get_deployment_by_name = AsyncMock(return_value=deployment)

        project_store = MagicMock()
        project_store.get = MagicMock(return_value=MagicMock(filename="mpfm-w3h.yaml"))

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("opi.manager.delete_project_manager.get_project_store", return_value=project_store),
            patch("os.path.exists", return_value=False),
        ):
            results = await DeleteProjectManager(pm).delete_deployment("mpfm-w3h", "pr-310", force=True)

        assert "remove_finalizers" not in calls
        gitops = next(op for op in results["operations"] if op["type"] == "argocd_app_gitops_deletion")
        assert gitops["finalizer_removed"] is False
        assert any("left standing with its finalizer" in error for error in results["errors"])

    @pytest.mark.asyncio
    async def test_yaml_change_delete_clears_the_operation_before_waiting(self) -> None:
        calls: list[str] = []
        pm, argo, project_data, deployment = _deployment_harness(calls)

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("os.path.exists", return_value=False),
        ):
            await DeleteProjectManager(pm).delete_deployment_from_yaml_change("mpfm-w3h", deployment, project_data)

        assert calls[:2] == ["terminate_operation", "wait_for_deletion"]
        assert _targets(pm, "terminate_argocd_application_operation") == ["mpfm-w3h-pr-310"]

    @pytest.mark.asyncio
    async def test_orphan_cleanup_clears_the_operation_before_deleting(self) -> None:
        """The orphan cleanup deletes directly instead of via GitOps, and hits the same gate."""
        calls: list[str] = []
        pm, argo = _orphan_cleanup_harness(calls)

        results: dict = {"operations": [], "errors": []}
        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("opi.manager.delete_project_manager.get_argo_namespace", return_value="argocd-test"),
        ):
            await DeleteProjectManager(pm)._cleanup_orphaned_argocd_resources("mpfm-w3h", results)

        assert calls == ["terminate_operation", "delete_application"]
        assert _targets(pm, "terminate_argocd_application_operation") == ["mpfm-w3h-pr-310"]
        assert pm._kubectl_connector.delete_argocd_application.await_args.args == (
            "mpfm-w3h-pr-310",
            "argocd-test",
        )
        assert results["errors"] == []
        assert [operation["status"] for operation in results["operations"]] == ["success", "success"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("deleted", "status"), [(True, "success"), (False, "failed")])
    async def test_orphan_cleanup_reports_whether_the_application_actually_went(
        self, deleted: bool, status: str
    ) -> None:
        """This route has no GitOps manifest to take away, so its own delete IS the
        deletion. A kubectl that answers no leaves the orphan standing, and reporting that
        as success is what makes it invisible."""
        calls: list[str] = []
        pm, argo = _orphan_cleanup_harness(calls, deleted=deleted)

        results: dict = {"operations": [], "errors": []}
        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("opi.manager.delete_project_manager.get_argo_namespace", return_value="argocd-test"),
        ):
            await DeleteProjectManager(pm)._cleanup_orphaned_argocd_resources("mpfm-w3h", results)

        cleanup = next(op for op in results["operations"] if op["type"] == "orphaned_argocd_application_cleanup")
        assert (cleanup["target"], cleanup["status"]) == ("mpfm-w3h-pr-310", status)


class TestProjectFileFollowsTheCluster:
    """RC-238: de project file kreeg delete-commits terwijl de resources nog stonden.

    Op 29 september landden de commits om 14:42 terwijl de cascade nog liep. De file is
    de desired state die elke volgende run leest, dus daarna vroeg niets aan beide kanten
    nog om de verwijdering: de deployment was weg uit de file en aanwezig op het cluster.
    """

    @staticmethod
    def _harness(calls: list[str], *, wait_results: list[bool] | None = None) -> tuple[AsyncMock, AsyncMock, MagicMock]:
        pm, argo, project_data, deployment = _deployment_harness(calls, wait_results=wait_results)
        pm.get_contents = AsyncMock(return_value=project_data)
        pm.get_deployment_by_name = AsyncMock(return_value=deployment)
        project_store = MagicMock()
        project_store.get = MagicMock(return_value=MagicMock(filename="mpfm-w3h.yaml"))
        return pm, argo, project_store

    @pytest.mark.asyncio
    async def test_a_confirmed_delete_still_commits_the_removal(self) -> None:
        """Het happy path blijft onveranderd, en zonder deze helft meet de toets eronder
        alleen dat er niet gecommit wordt."""
        calls: list[str] = []
        pm, argo, project_store = self._harness(calls, wait_results=[True])

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("opi.manager.delete_project_manager.get_project_store", return_value=project_store),
            patch("os.path.exists", return_value=False),
        ):
            results = await DeleteProjectManager(pm).delete_deployment("mpfm-w3h", "pr-310")

        pm.mutate_and_commit_project.assert_awaited_once()
        bestand = next(op for op in results["operations"] if op["type"] == "project_file_update")
        assert (bestand["action"], bestand["status"]) == ("removed_from_project_file", "success")

    @pytest.mark.asyncio
    async def test_an_unconfirmed_delete_does_not_commit_and_fails_red(self) -> None:
        """De kern: geen commit, de entry blijft staan, en de taak faalt.

        ``force=False`` dus de force-route komt er niet aan te pas: dit is de delete die
        afliep zonder bevestiging, precies de vorm uit het incident.
        """
        calls: list[str] = []
        pm, argo, project_store = self._harness(calls, wait_results=[False])

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("opi.manager.delete_project_manager.get_project_store", return_value=project_store),
            patch("os.path.exists", return_value=False),
        ):
            results = await DeleteProjectManager(pm).delete_deployment("mpfm-w3h", "pr-310")

        pm.mutate_and_commit_project.assert_not_awaited()
        bestand = next(op for op in results["operations"] if op["type"] == "project_file_update")
        assert (bestand["action"], bestand["status"]) == ("removed_from_project_file", "skipped")
        assert bestand["reason"].startswith("ArgoCD application not confirmed deleted")
        assert any("kept in the project file" in error for error in results["errors"])
        # De taakhandler faalt de taak op een niet-lege errors-lijst, en dat is wat de
        # delete opnieuw aanvraagbaar houdt.
        assert results["success"] is False

    @pytest.mark.asyncio
    async def test_the_yaml_change_route_never_writes_the_file_back(self) -> None:
        """De andere helft van de eis uit het plan: deze route commit niets.

        Hier heeft de gebruiker de entry zelf uit de file gehaald, dus de file is de
        INVOER. Dezelfde grendel hier aanbrengen zou een tweede schrijver op hetzelfde
        veld zetten, en dat is precies wat de twee kanten uit elkaar liet lopen.
        """
        calls: list[str] = []
        pm, argo, project_data, deployment = _deployment_harness(calls, wait_results=[False])

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("os.path.exists", return_value=False),
        ):
            results = await DeleteProjectManager(pm).delete_deployment_from_yaml_change(
                "mpfm-w3h", deployment, project_data
            )

        pm.mutate_and_commit_project.assert_not_awaited()
        assert not [op for op in results["operations"] if op["type"] == "project_file_update"]


class TestTheProgressWindowReachesTheRoutes:
    """RC-238: de voortgangsbewuste wacht is opt-in, dus de aanroep is de helft van de fix.

    ``wait_for_application_deletion`` valt zonder ``progress_window_seconds`` terug op het
    oude gedrag: stoppen op ``max_retries``, voortgang of niet. Dat is precies de stand van
    29 september. De drie routes die een Application met een workload laten verwijderen
    moeten het venster dus meegeven, en zonder deze toetsen is elk van die regels los te
    verwijderen zonder dat er iets rood wordt.
    """

    @staticmethod
    def _window_of(argo: AsyncMock, index: int = 0) -> int | None:
        return argo.wait_for_application_deletion.await_args_list[index].kwargs.get("progress_window_seconds")

    @pytest.mark.asyncio
    async def test_the_deployment_delete_waits_on_progress(self) -> None:
        calls: list[str] = []
        pm, argo, project_data, deployment = _deployment_harness(calls)
        pm.get_contents = AsyncMock(return_value=project_data)
        pm.get_deployment_by_name = AsyncMock(return_value=deployment)
        project_store = MagicMock()
        project_store.get = MagicMock(return_value=MagicMock(filename="mpfm-w3h.yaml"))

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("opi.manager.delete_project_manager.get_project_store", return_value=project_store),
            patch("os.path.exists", return_value=False),
        ):
            await DeleteProjectManager(pm).delete_deployment("mpfm-w3h", "pr-310")

        assert self._window_of(argo) == DELETION_PROGRESS_BUDGET_SECONDS

    @pytest.mark.asyncio
    async def test_the_yaml_change_delete_waits_on_progress(self) -> None:
        calls: list[str] = []
        pm, argo, project_data, deployment = _deployment_harness(calls)

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("os.path.exists", return_value=False),
        ):
            await DeleteProjectManager(pm).delete_deployment_from_yaml_change("mpfm-w3h", deployment, project_data)

        assert self._window_of(argo) == DELETION_PROGRESS_BUDGET_SECONDS

    @pytest.mark.asyncio
    async def test_the_infrastructure_delete_waits_on_progress(self) -> None:
        """De infra-Application hangt onder zich een CNPG-cluster, en die cascade is de
        langzaamste van de drie."""
        calls: list[str] = []
        pm, argo, project_data, _ = _deployment_harness(calls)
        results: dict = {"operations": [], "errors": []}

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("os.path.exists", return_value=False),
        ):
            await DeleteProjectManager(pm)._cleanup_project_infrastructure(
                "mpfm-w3h",
                "local",
                {**project_data, "services": ["namespace-postgresql-database"]},
                results,
                force=False,
            )

        assert self._window_of(argo) == DELETION_PROGRESS_BUDGET_SECONDS

    @pytest.mark.asyncio
    async def test_the_wait_after_a_forced_finalizer_removal_has_no_progress_window(self) -> None:
        """De tweede wacht is een andere wacht, en die hoort kort te blijven.

        Daar is de finalizer net weggehaald, dus de Application wordt door de garbage
        collector opgeruimd en niet meer door een cascade. Het venster daar ook op zetten
        maakt van een mislukte opruiming een wacht van tien minuten.
        """
        calls: list[str] = []
        pm, argo, project_data, deployment = _deployment_harness(
            calls, wait_results=[False, True], tracked=[_tracked("mpfm-w3h-pr-310", "db-creds")]
        )
        pm.get_contents = AsyncMock(return_value=project_data)
        pm.get_deployment_by_name = AsyncMock(return_value=deployment)
        project_store = MagicMock()
        project_store.get = MagicMock(return_value=MagicMock(filename="mpfm-w3h.yaml"))

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("opi.manager.delete_project_manager.get_project_store", return_value=project_store),
            patch("os.path.exists", return_value=False),
        ):
            await DeleteProjectManager(pm).delete_deployment("mpfm-w3h", "pr-310", force=True)

        assert calls.count("wait_for_deletion") == 2
        assert self._window_of(argo, 0) == DELETION_PROGRESS_BUDGET_SECONDS
        assert self._window_of(argo, 1) is None

    @pytest.mark.asyncio
    async def test_the_second_deployment_of_a_task_gets_what_is_left_of_the_budget(self) -> None:
        """De route moet het budget AANROEPEN en niet de constante meegeven.

        ``delete_project`` verwijdert zijn deployments in een lus, en daar zit het verschil
        tussen een budget per taak en een venster per wacht. Met de constante op alle drie de
        aanroepregels bleven de twee toetsbestanden van deze PR groen: de budgetfunctie werd
        los getoetst, haar aanroep niet.
        """
        calls: list[str] = []
        pm, argo, project_data, deployment = _deployment_harness(calls)
        pm.get_contents = AsyncMock(return_value=project_data)
        pm.get_deployment_by_name = AsyncMock(return_value=deployment)
        project_store = MagicMock()
        project_store.get = MagicMock(return_value=MagicMock(filename="mpfm-w3h.yaml"))

        async def _twee_deployments_in_een_taak() -> None:
            with (
                patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
                patch("opi.manager.delete_project_manager.get_project_store", return_value=project_store),
                patch("os.path.exists", return_value=False),
            ):
                manager = DeleteProjectManager(pm)
                await manager.delete_deployment("mpfm-w3h", "pr-310")
                await manager.delete_deployment("mpfm-w3h", "pr-311")

        await asyncio.create_task(_twee_deployments_in_een_taak())

        eerste, tweede = self._window_of(argo, 0), self._window_of(argo, 1)
        assert eerste == DELETION_PROGRESS_BUDGET_SECONDS
        assert tweede is not None
        assert tweede < DELETION_PROGRESS_BUDGET_SECONDS


class TestTheProgressBudgetIsPerTask:
    """RC-238 review r1: een venster per WACHT past niet in de taak die de wachten doet.

    ``TASK_WORKER_MAX_DURATION`` is 1800s en kapt de handler HARD af: geen force-route, geen
    resultaat, waar een delete voorheen netjes rood afliep. Twee van de drie aanroepers staan
    in een lus over de deployments, dus het venster hoort een budget van de TAAK te zijn.
    """

    @staticmethod
    async def _vensters(aantal: int) -> list[int | None]:
        """De vensters van opeenvolgende wachten binnen EEN taak."""
        return [deletion_progress_window() for _ in range(aantal)]

    @pytest.mark.asyncio
    async def test_the_first_wait_gets_the_whole_budget(self) -> None:
        assert await asyncio.create_task(self._vensters(1)) == [DELETION_PROGRESS_BUDGET_SECONDS]

    @pytest.mark.asyncio
    async def test_a_second_wait_in_the_same_task_gets_less_than_the_first(self) -> None:
        """De kern: het budget wordt OPGEBRUIKT. Zou elke wacht het volle venster krijgen,
        dan is drie deployments verwijderen 3 x 600s aan puur wachten binnen een taak van
        1800s, en dat is precies de grens die de handler midden in de delete afkapt."""
        vensters = await asyncio.create_task(self._vensters(2))

        assert vensters[0] == DELETION_PROGRESS_BUDGET_SECONDS
        assert vensters[1] is not None
        assert vensters[1] < DELETION_PROGRESS_BUDGET_SECONDS

    @pytest.mark.asyncio
    async def test_an_exhausted_budget_falls_back_to_the_plain_window(self) -> None:
        """Budget op: geen venster meer, dus die wacht loopt netjes rood af met de
        force-route erachter.

        De deadline wordt hier direct gezet. De klok van de module patchen patcht
        ``time.monotonic`` van de event loop mee, en die heeft hem zelf nodig.
        """

        async def _na_het_budget() -> int | None:
            _DELETION_PROGRESS_DEADLINE.set(time.monotonic() - 1)
            return deletion_progress_window()

        assert await asyncio.create_task(_na_het_budget()) is None

    @pytest.mark.asyncio
    async def test_every_task_gets_its_own_budget(self) -> None:
        """Per taak en niet per proces: de volgende delete-taak begint weer met het hele
        budget, anders zou de eerste taak van de dag het voor alle volgende opmaken."""
        eerste = await asyncio.create_task(self._vensters(2))
        tweede = await asyncio.create_task(self._vensters(1))

        assert eerste[1] != DELETION_PROGRESS_BUDGET_SECONDS
        assert tweede == [DELETION_PROGRESS_BUDGET_SECONDS]

    @pytest.mark.asyncio
    async def test_the_window_is_what_the_clock_says_is_left(self) -> None:
        """Niet alleen "minder dan het budget", maar hoeveel minder.

        Hoe groot het venster is, is de hele inhoud van deze grens: te klein en een trage
        cascade wordt weer op de klok afgekapt, te groot en de taakgrens kapt de handler af.
        Zonder deze toets is de rekenregel vrij te verschalen zolang de uitkomst nog onder
        het budget blijft: halveren gaf 0 rood over de twee toetsbestanden van deze PR.
        """

        async def _met_420_seconden_over() -> int | None:
            _DELETION_PROGRESS_DEADLINE.set(time.monotonic() + 420)
            return deletion_progress_window()

        venster = await asyncio.create_task(_met_420_seconden_over())

        assert venster is not None
        # Afgerond naar beneden, en de meting zelf kost ook iets.
        assert 418 <= venster <= 420


# ---------------------------------------------------------------------------
# RC-255 / #205: the manifest folder after a sweep that could not get everything
# ---------------------------------------------------------------------------

MANIFEST_FOLDER = "/tmp/gitops/local/mpfm-w3h/pr-310"


async def _delete_a_half_swept_deployment(calls: list[str], *, cascade_ends: bool) -> dict:
    """A forced deployment delete whose sweep is partial and whose manifest folder exists.

    That is every forced delete in a Capsule tenant: the account may not list some 90 of the
    cluster's types, so the inventory is never complete and the sweep is always ``partial``.
    """
    pm, argo, project_data, deployment = _deployment_harness(
        calls,
        wait_results=[False, cascade_ends],
        tracked=[_tracked("mpfm-w3h-pr-310", "db-creds")],
        complete=False,
    )
    pm.get_contents = AsyncMock(return_value=project_data)
    pm.get_deployment_by_name = AsyncMock(return_value=deployment)

    project_store = MagicMock()
    project_store.get = MagicMock(return_value=MagicMock(filename="mpfm-w3h.yaml"))

    deferred = MagicMock()
    deferred.return_value.mark_resource = AsyncMock(return_value=True)

    subdomains = MagicMock()
    subdomains.return_value.delete_by_deployment = AsyncMock(return_value=0)

    with (
        patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
        patch("opi.manager.delete_project_manager.get_project_store", return_value=project_store),
        patch("opi.manager.delete_project_manager.SubdomainConnector", subdomains),
        patch("opi.services.marked_for_deletion_service.MarkedForDeletionService", deferred),
        patch("os.path.exists", side_effect=lambda path: path == MANIFEST_FOLDER),
        patch("opi.manager.delete_project_manager.shutil.rmtree") as rmtree,
    ):
        results = await DeleteProjectManager(pm).delete_deployment("mpfm-w3h", "pr-310", force=True)

    results["_rmtree"] = rmtree
    results["_deferred"] = deferred
    results["_argo"] = argo
    return results


class TestTheManifestFolderAfterAPartialSweep:
    """#205: the deployments-repo was the one of four places that stayed dirty.

    Measured on odcn-production on 9 October 2026 over 15 deletes: cluster, project file and
    argo-repo clean every time, the deployments-repo clean 13 times. The two that stayed
    behind are this: the sweep is partial in a tenant, so the finalizer stays, so
    ``argocd_app_deleted`` stays False, so step 7 skips the folder. ArgoCD finished the
    cascade itself minutes later, but by then the task had already reported failure.
    """

    @pytest.mark.asyncio
    async def test_the_cascade_is_waited_for_and_the_folder_goes(self) -> None:
        calls: list[str] = []
        results = await _delete_a_half_swept_deployment(calls, cascade_ends=True)

        # The sweep deleted what it could read, the finalizer stayed, and the wait that
        # follows is for the cascade that finalizer carries.
        assert calls == [
            "terminate_operation",
            "wait_for_deletion",
            "read_namespace",
            "list_resources",
            "list_applications",
            "delete_resources",
            "wait_for_deletion",
        ]
        cascade = next(op for op in results["operations"] if op["type"] == "argocd_app_cascade_wait")
        assert cascade["status"] == "success"
        results["_rmtree"].assert_called_once_with(MANIFEST_FOLDER)
        folder = next(op for op in results["operations"] if op["type"] == "deployment_folder_deletion")
        assert folder["status"] == "success"

    @pytest.mark.asyncio
    async def test_a_delete_that_ends_clean_is_reported_as_a_success(self) -> None:
        """The other half of the same fix. The reasons the sweep kept the finalizer are
        errors, and ``success`` is ``errors == []``, so leaving them standing reports a
        failure for a deployment that is demonstrably gone, and nobody looks again.
        """
        results = await _delete_a_half_swept_deployment([], cascade_ends=True)

        assert results["errors"] == []
        assert results["success"] is True
        # Withdrawn, not forgotten: a partial sweep stays in the history as an operation.
        cascade = next(op for op in results["operations"] if op["type"] == "argocd_app_cascade_wait")
        assert any("left standing with its finalizer" in error for error in cascade["withdrawn_errors"])
        sweep = next(op for op in results["operations"] if op["type"] == "argocd_app_tracked_resource_deletion")
        assert sweep["status"] == "partial"

    @pytest.mark.asyncio
    async def test_an_application_that_outlasts_the_cascade_wait_keeps_its_folder(self) -> None:
        """The guard this fix may not soften. An Application that is still there needs its
        source path: its finalizer reads it to work out what to delete. So the folder is
        marked for deferred cleanup and the delete reports a failure.
        """
        calls: list[str] = []
        results = await _delete_a_half_swept_deployment(calls, cascade_ends=False)

        cascade = next(op for op in results["operations"] if op["type"] == "argocd_app_cascade_wait")
        assert cascade["status"] == "timeout"
        results["_rmtree"].assert_not_called()
        folder = next(op for op in results["operations"] if op["type"] == "deployment_folder_deletion")
        assert folder["status"] == "deferred"
        assert results["_deferred"].return_value.mark_resource.await_args.kwargs["resource_name"] == (
            "local/mpfm-w3h/pr-310"
        )
        assert results["success"] is False
        assert any("left standing with its finalizer" in error for error in results["errors"])

    @pytest.mark.asyncio
    async def test_the_cascade_wait_outlasts_the_cascades_measured_on_production(self) -> None:
        """All 15 were gone within a few minutes; a wait shorter than that hands the folder
        to a nightly reconciliation that runs as a dry-run in production.

        Measured on what the CALL hands over, bound against the connector's own signature
        so the defaults count: ``wait_for_application_deletion`` falls back to 5 x 3s, so a
        call that passes only ``max_retries`` waits 15s while the constants still read 180.
        """
        results = await _delete_a_half_swept_deployment([], cascade_ends=True)

        cascade_wait = results["_argo"].wait_for_application_deletion.await_args_list[1]
        bound = signature(ArgoConnector.wait_for_application_deletion).bind(
            None, *cascade_wait.args, **cascade_wait.kwargs
        )
        bound.apply_defaults()
        assert bound.arguments["max_retries"] * bound.arguments["retry_delay"] >= 180
        assert (bound.arguments["max_retries"], bound.arguments["retry_delay"]) == (
            CASCADE_WAIT_RETRIES,
            CASCADE_WAIT_DELAY,
        )
