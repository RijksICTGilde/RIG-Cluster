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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.argo import ArgoConnector
from opi.connectors.kubectl import KubectlConnector
from opi.manager.delete_project_manager import DeleteProjectManager
from opi.utils.argocd_tracking import (
    INSTANCE_LABEL,
    LABEL_VALUE_MAX,
    TRACKING_ID_ANNOTATION,
    TrackedResource,
    application_name_from_tracking_id,
    is_tracked_by,
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
        assert resource.app_name == "app-a"

    def test_the_annotation_wins_over_a_label(self) -> None:
        """With annotation tracking the label may be a leftover from another tool."""
        resource = tracked_resource_from_item(
            _item("Secret", "v1", "db-creds", "app-a:/Secret:ns/db-creds", instance_label="something-else")
        )
        assert resource is not None
        assert resource.app_name == "app-a"

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


class TestIsTrackedBy:
    #: A name longer than a label value can hold, in the shape the schema allows.
    LONG_APP = "a" * 30 + "-" + "b" * 63

    def test_the_same_name_matches(self) -> None:
        assert is_tracked_by("app-a", {"app-a", "app-b"}) is True

    def test_another_name_does_not(self) -> None:
        assert is_tracked_by("app-c", {"app-a", "app-b"}) is False

    def test_a_value_at_the_cap_matches_the_longer_name_it_starts(self) -> None:
        assert len(self.LONG_APP) > LABEL_VALUE_MAX
        assert is_tracked_by(self.LONG_APP[:LABEL_VALUE_MAX], {self.LONG_APP}) is True

    def test_a_value_at_the_cap_still_needs_an_application_to_belong_to(self) -> None:
        """Otherwise the cap would turn every long orphan into something to leave alone."""
        assert is_tracked_by("z" * LABEL_VALUE_MAX, {self.LONG_APP}) is False

    def test_a_value_below_the_cap_is_not_a_prefix_match(self) -> None:
        """``mpfm-w3h-pr-31`` was never cut, so ``mpfm-w3h-pr-310`` does not protect it."""
        assert is_tracked_by("mpfm-w3h-pr-31", {"mpfm-w3h-pr-310"}) is False

    def test_a_value_over_the_cap_is_not_a_prefix_match(self) -> None:
        assert is_tracked_by(self.LONG_APP[:-1], {self.LONG_APP}) is False


# ---------------------------------------------------------------------------
# The connector: the manual repair that worked, as code
# ---------------------------------------------------------------------------


class TestTerminateOperation:
    @pytest.mark.asyncio
    async def test_sends_the_json_patch_that_removes_the_operation(self, connector) -> None:
        """The measured repair: remove /operation, not the finalizer."""
        run = AsyncMock(return_value=("patched", "", 0))
        with patch.object(connector, "_run_kubectl_command", run):
            assert await connector.terminate_argocd_application_operation("mpfm-w3h-pr-310", "argocd") is True

        args = run.await_args.args[0]
        assert args[:5] == ["patch", "application", "mpfm-w3h-pr-310", "-n", "argocd"]
        assert "--type" in args
        assert args[args.index("--type") + 1] == "json"
        assert args[-1] == '[{"op":"remove","path":"/operation"}]'

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stderr",
        [
            'remove operation does not apply: doc is missing path: "/operation": missing value',
            'doc is missing path: "/operation"',
            "remove operation does not apply",
        ],
    )
    async def test_no_operation_running_is_not_a_failure(self, connector, stderr: str) -> None:
        """A json-patch remove on an absent path is rejected; that is the normal case.

        The branch reads two spellings and each one counts on its own. Narrowing it to one
        turns 'nothing was running' into a reported failure on the most common path there
        is: an Application with no operation on it.
        """
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", stderr, 1))):
            assert await connector.terminate_argocd_application_operation("app-a") is True

    @pytest.mark.asyncio
    async def test_missing_application_is_not_a_failure(self, connector) -> None:
        stderr = 'Error from server (NotFound): applications.argoproj.io "app-a" not found'
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", stderr, 1))):
            assert await connector.terminate_argocd_application_operation("app-a") is True

    @pytest.mark.asyncio
    async def test_any_other_error_is_a_failure(self, connector) -> None:
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", "connection reset", 1))):
            assert await connector.terminate_argocd_application_operation("app-a") is False


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
    async def test_a_failed_discovery_is_not_an_empty_cluster(self, connector) -> None:
        """Reading this as 'no types' makes every namespace inventory as empty (RC-226)."""
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("", "boom", 1))):
            assert await connector.list_namespaced_resource_types() is None


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
            tracked = await connector.list_tracked_resources("rig-prd-mpfm-w3h", ["secrets", "deployments.apps"])

        assert [(r.name, r.app_name) for r in tracked] == [("db-creds", "app-a"), ("web", "app-b")]

    @pytest.mark.asyncio
    async def test_partial_output_is_kept(self, connector) -> None:
        """kubectl exits non-zero when ONE queried type fails, but prints the rest.

        Dropping that would report an empty namespace while resources are still standing.
        """
        import json

        payload = {"items": [_item("Secret", "v1", "db-creds", "app-a:/Secret:ns/db-creds")]}
        stderr = "error: unable to retrieve the complete list of server APIs: metrics.k8s.io/v1beta1"
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(json.dumps(payload), stderr, 1))):
            tracked = await connector.list_tracked_resources("rig-prd-mpfm-w3h", ["secrets"])

        assert [r.name for r in tracked] == ["db-creds"]

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
            assert await connector.list_tracked_resources("ns", ["secrets"]) == []

    @pytest.mark.asyncio
    async def test_unparsable_output_is_not_read_as_empty_silently(self, connector) -> None:
        with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=("not json", "", 0))):
            assert await connector.list_tracked_resources("ns", ["secrets"]) is None


class TestDeleteTrackedResources:
    @staticmethod
    def _resource(name: str) -> TrackedResource:
        return TrackedResource(
            kind="Secret",
            api_version="v1",
            name=name,
            namespace="rig-prd-mpfm-w3h",
            app_name="app-a",
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
    calls: list[str], *, destination: str | None, tracked: list[TrackedResource] | None, deleted: bool = True
) -> AsyncMock:
    """A kubectl stand-in that writes the name of each call into ``calls``.

    ``spec_set=KubectlConnector`` for the same reason the ArgoConnector mock below carries a
    spec, and ``spec_set`` rather than ``spec`` because here it is the stubbing that has to
    fail, which only ``spec_set`` restricts.
    """

    def step(name: str, result):
        async def _run(*args, **kwargs):
            calls.append(name)
            return result

        return AsyncMock(side_effect=_run)

    kubectl = AsyncMock(spec_set=KubectlConnector)
    kubectl.terminate_argocd_application_operation = step("terminate_operation", True)
    kubectl.get_argocd_application_destination_namespace = step("read_namespace", destination)
    kubectl.list_tracked_resources = step("list_resources", tracked)
    kubectl.delete_tracked_resources = step("delete_resources", [])
    kubectl.remove_argocd_application_finalizers = step("remove_finalizers", True)
    kubectl.delete_argocd_application = step("delete_application", deleted)
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


def _tracked(app_name: str, name: str) -> TrackedResource:
    return TrackedResource(
        kind="Secret",
        api_version="v1",
        name=name,
        namespace="rig-prd-mpfm-w3h",
        app_name=app_name,
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

        assert await DeleteProjectManager(pm)._force_delete_stuck_application("app-a", results) is True

        assert calls == ["read_namespace", "list_resources", "delete_resources", "remove_finalizers"]

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

    @pytest.mark.asyncio
    async def test_a_resource_whose_label_was_cut_to_the_cap_is_still_taken(self) -> None:
        """Otherwise the force reports ``deleted: 0`` without an error and leaves them.

        That is the silent force this task is about, arriving through the other door: not a
        failed inventory, but a successful one whose names do not compare equal.
        """
        app = "a" * 30 + "-" + "b" * 63
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(
            [], destination="rig-prd-mpfm-w3h", tracked=[_tracked(app[:LABEL_VALUE_MAX], "db-creds")]
        )
        results: dict = {"operations": [], "errors": []}

        await DeleteProjectManager(pm)._force_delete_stuck_application(app, results)

        deleted = pm._kubectl_connector.delete_tracked_resources.await_args.args[0]
        assert [r.name for r in deleted] == ["db-creds"]

    @pytest.mark.asyncio
    async def test_reports_how_much_it_deleted(self) -> None:
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl(
            [], destination="rig-prd-mpfm-w3h", tracked=[_tracked("app-a", "a"), _tracked("app-a", "b")]
        )
        results: dict = {"operations": [], "errors": []}

        await DeleteProjectManager(pm)._force_delete_stuck_application("app-a", results)

        operation = next(op for op in results["operations"] if op["type"] == "argocd_app_tracked_resource_deletion")
        assert operation["status"] == "success"
        assert operation["deleted"] == 2
        assert operation["namespace"] == "rig-prd-mpfm-w3h"

    @pytest.mark.asyncio
    async def test_resources_that_would_not_go_become_an_error(self) -> None:
        pm = AsyncMock()
        pm._kubectl_connector = _recording_kubectl([], destination="ns", tracked=[_tracked("app-a", "a")])
        pm._kubectl_connector.delete_tracked_resources = AsyncMock(return_value=[_tracked("app-a", "a")])
        results: dict = {"operations": [], "errors": []}

        await DeleteProjectManager(pm)._force_delete_stuck_application("app-a", results)

        operation = next(op for op in results["operations"] if op["type"] == "argocd_app_tracked_resource_deletion")
        assert operation["status"] == "partial"
        assert results["errors"]

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

        assert await DeleteProjectManager(pm)._force_delete_stuck_application("app-a", results) is True

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

        assert await DeleteProjectManager(pm)._force_delete_stuck_application("app-a", results) is True

        assert calls == ["read_namespace", "list_resources", "remove_finalizers"]
        pm._kubectl_connector.delete_tracked_resources.assert_not_awaited()
        operation = next(op for op in results["operations"] if op["type"] == "argocd_app_tracked_resource_deletion")
        assert operation["status"] == "inventory_failed"
        assert any("Could not inventory namespace" in error for error in results["errors"])


def _deployment_harness(
    calls: list[str], *, wait_results: list[bool] | None = None, tracked: list[TrackedResource] | None = None
) -> tuple[AsyncMock, AsyncMock, dict, dict]:
    """A deployment delete with a live Application, recording the order of the calls.

    ``wait_results`` feeds successive answers to the deletion wait, so a caller can make
    the first wait time out and reach the force branch.
    """
    pm = AsyncMock()
    pm._kubectl_connector = _recording_kubectl(calls, destination="rig-mpfm-w3h", tracked=tracked or [])
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

        assert calls[:7] == [
            "terminate_operation",
            "wait_for_deletion",
            "read_namespace",
            "list_resources",
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
