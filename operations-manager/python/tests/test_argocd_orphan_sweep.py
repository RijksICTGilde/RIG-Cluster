"""Tests for the sweep that finds what a deleted Application left behind (RC-226).

The sweep decides, per resource, whether to delete it. Two things therefore have to be
exactly right, and both are about NOT deleting too much:

- the test is "its Application does not exist", so a live Application protects its
  resources, including one whose name merely resembles another;
- a repo path an Application points at is not an orphan, whatever its shape.

The ad-hoc version was run against rig-prd-mpfm-w3h on 24 September 2026 and found the
ten cases that had been worked out by hand, no more and no fewer, while the five live
environments stayed out of range. That is the behaviour pinned here.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from opi.connectors.kubectl import KubectlConnectionError, KubectlConnector, KubectlExecutionError
from opi.utils.argocd_tracking import LABEL_VALUE_MAX, TrackedResource
from scripts.argocd_orphan_sweep import (
    CLEAN,
    SweepRefused,
    expected_render_roots,
    format_report,
    helmfile_namespaces,
    helmfile_projects,
    inventory,
    main,
    orphaned_applications,
    orphaned_paths,
    orphaned_resources,
    remove,
    render_roots,
    rendered_kinds,
    rendered_types,
    undecidable_resources,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKFILE = REPO_ROOT / "Taskfile.yaml"
SWEEP_TASK = "argocd-orphan-sweep"


def _resource(
    app_name: str,
    name: str,
    kind: str = "Secret",
    namespace: str = "rig-prd-mpfm-w3h",
    from_label: bool = False,
) -> TrackedResource:
    return TrackedResource(
        kind=kind,
        api_version="v1",
        name=name,
        namespace=namespace,
        app_name=app_name,
        from_label=from_label,
    )


#: 63 characters: a whole Application name, and also what a longer name is cut to in a label
#: value. ``LONG_APP`` is the longer one, in the shape the schema allows (30 + 63).
CUT_NAME = "p-" + "b" * 61
LONG_APP = CUT_NAME + "-x"


class TestOrphanedResources:
    def test_a_resource_of_a_vanished_application_is_an_orphan(self) -> None:
        orphans = orphaned_resources([_resource("mpfm-w3h-pr-310", "db-creds")], {"mpfm-w3h-productie"})
        assert [r.name for r in orphans] == ["db-creds"]

    def test_a_resource_of_a_live_application_is_left_alone(self) -> None:
        orphans = orphaned_resources([_resource("mpfm-w3h-productie", "db-creds")], {"mpfm-w3h-productie"})
        assert orphans == []

    def test_matching_is_on_the_whole_name(self) -> None:
        """``mpfm-w3h-pr-31`` is not protected by the existence of ``mpfm-w3h-pr-310``."""
        orphans = orphaned_resources([_resource("mpfm-w3h-pr-31", "db-creds")], {"mpfm-w3h-pr-310"})
        assert [r.app_name for r in orphans] == ["mpfm-w3h-pr-31"]

    def test_without_any_application_everything_tracked_is_an_orphan(self) -> None:
        tracked = [_resource("app-a", "a"), _resource("app-b", "b")]
        assert orphaned_resources(tracked, set()) == tracked

    def test_a_label_that_may_be_a_live_name_cut_to_the_cap_is_not_an_orphan(self) -> None:
        """The destructive direction: ``--delete`` acts on whatever this returns."""
        assert len(CUT_NAME) == LABEL_VALUE_MAX
        orphans = orphaned_resources([_resource(CUT_NAME, "db-creds", from_label=True)], {LONG_APP})
        assert orphans == []

    def test_an_annotation_at_that_length_is_a_whole_name_and_so_an_orphan(self) -> None:
        """Only a label can be cut. On odcn-production, the one cluster type that tracks by
        annotation, a name of this length is whole and its Application is simply gone."""
        orphans = orphaned_resources([_resource(CUT_NAME, "db-creds")], {LONG_APP})
        assert [r.name for r in orphans] == ["db-creds"]


class TestUndecidedResources:
    """The other half of the same doubt. Keeping such a resource out of the orphan list is
    right, reporting SCHOON over it is not: a real orphan would then stay invisible for as
    long as a longer-named sister lives."""

    def test_a_label_that_may_be_a_live_name_cut_to_the_cap_is_reported(self) -> None:
        undecidable = undecidable_resources([_resource(CUT_NAME, "db-creds", from_label=True)], {LONG_APP})
        assert [r.name for r in undecidable] == ["db-creds"]

    def test_a_resource_of_a_live_application_is_not_undecidable(self) -> None:
        """Its mark IS the name of a living Application, so whichever of the two readings
        holds, the resource is of something that is still running and no orphan. That is this
        caller's own condition: the predicate calls the same mark ambiguous, because the force
        deletes on it and there the longer sister matters."""
        assert undecidable_resources([_resource(CUT_NAME, "db-creds", from_label=True)], {CUT_NAME, LONG_APP}) == []

    def test_a_plain_orphan_is_not_undecidable(self) -> None:
        """It goes in the orphan list, where ``--delete`` can reach it."""
        assert undecidable_resources([_resource("mpfm-w3h-pr-310", "db-creds")], {"mpfm-w3h-productie"}) == []


class TestOrphanedPaths:
    @staticmethod
    def _repo(tmp_path: Path, *paths: str) -> Path:
        for path in paths:
            (tmp_path / path).mkdir(parents=True)
        return tmp_path

    def test_render_roots_are_the_cluster_project_leaf_directories(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path, "odcn-production/mpfm-w3h/pr-310", "odcn-production/mpfm-w3h/_project")
        assert render_roots(repo) == ["odcn-production/mpfm-w3h/_project", "odcn-production/mpfm-w3h/pr-310"]

    def test_a_deeper_directory_is_not_a_render_root_of_its_own(self, tmp_path: Path) -> None:
        """Only the third level is what an Application points at; what is under it belongs to it."""
        repo = self._repo(tmp_path, "odcn-production/mpfm-w3h/pr-310/base")
        assert render_roots(repo) == ["odcn-production/mpfm-w3h/pr-310"]

    def test_dot_directories_are_not_render_roots(self, tmp_path: Path) -> None:
        """``.git`` holds directories at exactly that depth; deleting one destroys the repo."""
        repo = self._repo(tmp_path, ".git/refs/heads", "odcn-production/mpfm-w3h/pr-310")
        assert render_roots(repo) == ["odcn-production/mpfm-w3h/pr-310"]

    def test_a_file_at_that_depth_is_not_a_render_root(self, tmp_path: Path) -> None:
        """A render root is a directory. Offering a file as one reaches ``--delete``, and
        ``shutil.rmtree`` on a file raises, so the sweep would die halfway."""
        repo = self._repo(tmp_path, "odcn-production/mpfm-w3h/pr-310")
        (tmp_path / "odcn-production/mpfm-w3h/kustomization.yaml").write_text("")
        assert render_roots(repo) == ["odcn-production/mpfm-w3h/pr-310"]

    def test_a_referenced_path_is_not_an_orphan(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path, "odcn-production/mpfm-w3h/pr-310")
        assert orphaned_paths(repo, {"odcn-production/mpfm-w3h/pr-310"}) == []

    def test_an_unreferenced_path_is_an_orphan(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path, "odcn-production/mpfm-w3h/pr-310", "odcn-production/mpfm-w3h/pr-9")
        assert orphaned_paths(repo, {"odcn-production/mpfm-w3h/pr-310"}) == ["odcn-production/mpfm-w3h/pr-9"]

    @pytest.mark.parametrize(
        "reference",
        [
            "odcn-production/mpfm-w3h/pr-310",
            "./odcn-production/mpfm-w3h/pr-310",
            "/odcn-production/mpfm-w3h/pr-310/",
        ],
    )
    def test_every_spelling_of_the_same_reference_protects(self, tmp_path: Path, reference: str) -> None:
        """The live Applications carry the ``./`` form (measured on the sandbox); OPI writes
        it bare. Comparing the two literally would call every live path an orphan."""
        repo = self._repo(tmp_path, "odcn-production/mpfm-w3h/pr-310")
        assert orphaned_paths(repo, {reference}) == []

    def test_a_reference_below_a_render_root_protects_it(self, tmp_path: Path) -> None:
        """A repository entry with a non-empty ``path`` puts the tree a level deeper than
        ``RENDER_ROOT_DEPTH``. Without this, every live project directory is an orphan and
        ``--delete`` takes them all."""
        repo = self._repo(tmp_path, "odcn-production/mpfm-w3h/pr-310/infra")
        assert orphaned_paths(repo, {"odcn-production/mpfm-w3h/pr-310/infra"}) == []

    def test_a_sibling_prefix_does_not_protect(self, tmp_path: Path) -> None:
        """``pr-31`` is a prefix of ``pr-310`` as a string, but not as a path."""
        repo = self._repo(tmp_path, "odcn-production/mpfm-w3h/pr-31", "odcn-production/mpfm-w3h/pr-310")
        assert orphaned_paths(repo, {"odcn-production/mpfm-w3h/pr-310"}) == ["odcn-production/mpfm-w3h/pr-31"]

    def test_an_application_without_a_path_protects_nothing(self, tmp_path: Path) -> None:
        """An empty string normalises to ``.``; reading that as a render root would
        silently protect a directory named after nothing."""
        repo = self._repo(tmp_path, "odcn-production/mpfm-w3h/pr-310")
        assert orphaned_paths(repo, {""}) == ["odcn-production/mpfm-w3h/pr-310"]


class TestReport:
    def test_nothing_found_is_reported_as_clean(self) -> None:
        """The last step of a delete test: it measures what is left, not what seemed to happen."""
        assert format_report([], [], []) == CLEAN

    def test_resources_are_grouped_under_their_application(self) -> None:
        report = format_report([_resource("app-a", "a"), _resource("app-b", "b"), _resource("app-a", "c")], [], [])
        assert "Found 3 resource(s)" in report
        assert "app-a  (2 resources in rig-prd-mpfm-w3h)" in report
        assert "app-b  (1 resources in rig-prd-mpfm-w3h)" in report

    def test_paths_are_reported_too(self) -> None:
        report = format_report([], [], ["odcn-production/mpfm-w3h/pr-9"])
        assert "1 deployments-repo path(s)" in report
        assert "odcn-production/mpfm-w3h/pr-9" in report

    def test_what_could_not_be_placed_gets_its_own_block(self) -> None:
        """Under the orphans it would read as something --delete took; it never touches these."""
        report = format_report([], [_resource(CUT_NAME, "db-creds", from_label=True)], [])
        assert report != CLEAN
        assert f"1 resource(s) whose mark sits on the {LABEL_VALUE_MAX}-character label cap" in report
        assert f"{CUT_NAME}  rig-prd-mpfm-w3h/secret/db-creds" in report

    def test_after_a_delete_the_report_says_what_is_still_standing(self) -> None:
        report = format_report([_resource("app-a", "a")], [], [], after_delete=True)
        assert report.startswith("Still standing 1 resource(s)")


#: One Application the cluster still runs. An empty list is a refusal, see TestRefusal.
_LIVE_APPLICATIONS = [{"metadata": {"name": "user-applications"}, "spec": {"source": {"path": "root"}}}]


def _kubectl(
    applications: list[dict],
    tracked: dict[str, list[TrackedResource]],
    namespace_labels: dict[str, str] | None = None,
) -> AsyncMock:
    """The kubectl the sweep reads the cluster through.

    ``spec_set`` because the sweep reads everything through this mock: a name that only
    exists on the mock is green in the suite and an AttributeError in the run.
    """
    kubectl = AsyncMock(spec_set=KubectlConnector)
    kubectl.list_argocd_applications = AsyncMock(return_value=applications)
    kubectl.get_namespace_label_map = AsyncMock(
        return_value=namespace_labels if namespace_labels is not None else dict.fromkeys(tracked, "operations-manager")
    )
    kubectl.list_namespaced_resource_types = AsyncMock(return_value=["secrets"])
    kubectl.list_namespaced_kinds = AsyncMock(return_value={"Secret": "secrets"})
    kubectl.list_tracked_resources = AsyncMock(side_effect=lambda ns, _types: (tracked.get(ns, []), True))
    kubectl.delete_tracked_resources = AsyncMock(return_value=[])
    return kubectl


class TestInventory:
    @pytest.mark.asyncio
    async def test_sweeps_every_namespace_when_none_is_named(self) -> None:
        """A namespace whose Applications are ALL gone is exactly where the leftovers are,
        so the list cannot be derived from the Applications that still exist."""
        kubectl = _kubectl(
            [{"metadata": {"name": "mpfm-w3h-productie"}, "spec": {"source": {"path": "p"}}}],
            {"rig-prd-mpfm-w3h": [_resource("mpfm-w3h-pr-310", "db-creds")], "rig-prd-other": []},
        )
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            resources, _, paths = await inventory(None, None)

        assert [r.name for r in resources] == ["db-creds"]
        assert paths == []
        assert [call.args[0] for call in kubectl.list_tracked_resources.await_args_list] == [
            "rig-prd-mpfm-w3h",
            "rig-prd-other",
        ]

    @pytest.mark.asyncio
    async def test_only_namespaces_opi_created_are_swept(self) -> None:
        """Helm writes ``app.kubernetes.io/instance`` too, and there it means the RELEASE.

        Measured on the sandbox, 24 September 2026: six resources in ``ingress-nginx``
        carry ``instance: ingress-nginx`` while no Application by that name exists. An
        allowlist of the namespaces OPI created is what keeps those out of range.
        """
        kubectl = _kubectl(
            _LIVE_APPLICATIONS,
            {"ingress-nginx": [_resource("ingress-nginx", "controller")], "rig-prd-mpfm-w3h": []},
            namespace_labels={"ingress-nginx": "", "rig-system": "", "rig-prd-mpfm-w3h": "operations-manager"},
        )
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            resources, _, _ = await inventory(None, None)

        assert resources == []
        assert [call.args[0] for call in kubectl.list_tracked_resources.await_args_list] == ["rig-prd-mpfm-w3h"]

    @pytest.mark.asyncio
    async def test_a_namespace_opi_did_not_create_is_refused_by_name_too(self) -> None:
        """Naming it by hand must not get past the allowlist either."""
        kubectl = _kubectl(
            _LIVE_APPLICATIONS,
            {"ingress-nginx": [_resource("ingress-nginx", "controller")]},
            namespace_labels={"ingress-nginx": "", "rig-prd-mpfm-w3h": "operations-manager"},
        )
        with (
            patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl),
            pytest.raises(SweepRefused, match="ingress-nginx"),
        ):
            await inventory(["ingress-nginx"], None)

        kubectl.list_tracked_resources.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_named_namespace_is_the_only_one_queried(self) -> None:
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": [], "rig-prd-other": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            await inventory(["rig-prd-mpfm-w3h"], None)

        assert [call.args[0] for call in kubectl.list_tracked_resources.await_args_list] == ["rig-prd-mpfm-w3h"]

    @pytest.mark.asyncio
    async def test_a_live_application_protects_its_own_resources(self) -> None:
        """The name that protects comes out of ``metadata.name`` of the Application CRs.

        The test itself lives in orphaned_resources, which is fed a ready-made set of
        names; reading that set out of the wrong field is what this covers. Every name
        would then be missing, every tracked resource would be an orphan, and with
        --delete a running deployment goes down. That is the one failure this tool may
        never have.
        """
        kubectl = _kubectl(
            [{"metadata": {"name": "mpfm-w3h-productie"}, "spec": {"source": {"path": "p"}}}],
            {"rig-prd-mpfm-w3h": [_resource("mpfm-w3h-productie", "db-creds")]},
        )
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            resources, _, _ = await inventory(None, None)

        assert resources == []

    @pytest.mark.asyncio
    async def test_an_application_with_several_sources_has_no_single_source_path(self, tmp_path: Path) -> None:
        """``spec.source`` can be present and null rather than absent, which is how an
        Application that uses ``spec.sources`` carries it. Reading ``.get("path")`` off
        that null ends the sweep in a traceback halfway through its inventory, so
        neither the report nor the exit code says anything.
        """
        (tmp_path / "odcn-production/mpfm-w3h/pr-9").mkdir(parents=True)
        kubectl = _kubectl(
            [{"metadata": {"name": "mpfm-w3h-productie"}, "spec": {"source": None, "sources": [{"path": "p"}]}}],
            {"rig-prd-mpfm-w3h": []},
        )
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            *_, paths = await inventory(["rig-prd-mpfm-w3h"], tmp_path)

        assert paths == ["odcn-production/mpfm-w3h/pr-9"]

    @pytest.mark.asyncio
    async def test_the_application_paths_come_from_the_cluster_not_from_argocd(self, tmp_path: Path) -> None:
        (tmp_path / "odcn-production/mpfm-w3h/pr-310").mkdir(parents=True)
        (tmp_path / "odcn-production/mpfm-w3h/pr-9").mkdir(parents=True)
        kubectl = _kubectl(
            [
                {
                    "metadata": {"name": "mpfm-w3h-pr-310"},
                    "spec": {"source": {"path": "odcn-production/mpfm-w3h/pr-310"}},
                }
            ],
            {"rig-prd-mpfm-w3h": []},
        )
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            *_, paths = await inventory(["rig-prd-mpfm-w3h"], tmp_path)

        assert paths == ["odcn-production/mpfm-w3h/pr-9"]
        kubectl.list_argocd_applications.assert_awaited()


class TestRefusal:
    @pytest.mark.asyncio
    async def test_an_empty_application_list_stops_the_sweep(self) -> None:
        """No Applications reads the same as a failed query, and both would make EVERY
        tracked resource an orphan. A platform cluster always runs user-applications."""
        kubectl = _kubectl([], {"rig-prd-mpfm-w3h": [_resource("app-a", "a")]})
        with (
            patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl),
            pytest.raises(SweepRefused),
        ):
            await inventory(None, None)

        kubectl.list_tracked_resources.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_type_discovery_stops_the_sweep(self) -> None:
        """Without the type list every namespace comes back empty, which would print
        SCHOON and exit 0 after querying nothing at all."""
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": [_resource("app-a", "a")]})
        kubectl.list_namespaced_resource_types = AsyncMock(return_value=None)
        with (
            patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl),
            pytest.raises(SweepRefused, match="resource types"),
        ):
            await inventory(None, None)

        kubectl.list_tracked_resources.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_namespace_that_could_not_be_inventoried_stops_the_sweep(self) -> None:
        """A namespace that did not answer is not an empty one. Reported as clean it is a
        false green on the test this tool exists for."""
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": [], "rig-prd-other": []})
        kubectl.list_tracked_resources = AsyncMock(
            side_effect=lambda ns, _types: None if ns == "rig-prd-other" else ([], True)
        )
        with (
            patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl),
            pytest.raises(SweepRefused, match="rig-prd-other"),
        ):
            await inventory(None, None)

    @pytest.mark.asyncio
    async def test_a_namespace_that_answered_only_in_part_stops_the_sweep_too(self) -> None:
        """Half an inventory is no more a measurement than none at all.

        The type list is discovered ONCE and handed to every namespace, and discovery reads
        the verbs of the CLUSTER, not what the service account may list in this namespace.
        One type it may not list and ``kubectl get`` prints the rest with exit 1, so the
        refusal on discovery above does not cover this. Measured with the connector handing
        a half answer over as complete: SCHOON on stdout and exit 0 over a namespace that
        answered for part of its types, which is the signal step 4 of the plan leans on.
        """
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": [], "rig-prd-other": []})
        kubectl.list_tracked_resources = AsyncMock(side_effect=lambda ns, _types: ([], ns != "rig-prd-other"))
        with (
            patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl),
            pytest.raises(SweepRefused, match="rig-prd-other"),
        ):
            await inventory(None, None)

    def test_a_namespace_that_answered_only_in_part_reaches_the_cli_as_exit_2(self, capsys) -> None:
        """Exit 0 plus SCHOON is what was wrong, so the refusal has to survive the way out."""
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": []})
        kubectl.list_tracked_resources = AsyncMock(return_value=([], False))
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h"]) == 2

        printed = capsys.readouterr()
        assert CLEAN not in printed.out
        assert "only part of its resource types" in printed.err

    def test_the_cli_answers_a_refusal_with_its_own_exit_code(self) -> None:
        """Not 0 (clean) and not 1 (orphans found): nothing was measured."""
        kubectl = _kubectl([], {})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h"]) == 2

    def test_an_unreadable_namespace_reaches_the_cli_as_exit_2(self) -> None:
        """The refusal has to survive the whole way out: exit 0 is what was wrong."""
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": []})
        kubectl.list_tracked_resources = AsyncMock(return_value=None)
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h"]) == 2

    def test_an_unreadable_namespace_list_reaches_the_cli_as_exit_2(self) -> None:
        """Exit 1 is the documented "there are orphans", so this read escaping would let a
        failed sweep answer the delete test it is the last step of."""
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": []})
        kubectl.get_namespace_label_map = AsyncMock(side_effect=KubectlExecutionError("Failed to list namespaces: x"))
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h"]) == 2

    def test_an_unreachable_cluster_reaches_the_cli_as_exit_2(self) -> None:
        """KubectlConnectionError is the failure every read in inventory() shares: raised at
        the start when isConnected is False (kubectl.py:229) and mid-run on a stderr saying
        'connection refused' (kubectl.py:297). Measured with the except narrowed back to
        SweepRefused alone: a traceback and exit 1, which means "there are orphans"."""
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": []})
        kubectl.list_argocd_applications = AsyncMock(
            side_effect=KubectlConnectionError("kubectl connection is not available")
        )
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h"]) == 2

    def test_a_cluster_that_drops_mid_run_reaches_the_cli_as_exit_2(self) -> None:
        """The other half: the connection survives the Application query and dies on the
        namespace inventory. Pinned separately because the first read failing is what
        stops the run in the test above, so it alone would leave this path unmeasured."""
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": []})
        kubectl.list_tracked_resources = AsyncMock(
            side_effect=KubectlConnectionError("kubectl connection failed: connection refused")
        )
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h"]) == 2

    def test_an_unreachable_cluster_is_not_reported_as_a_failed_namespace_read(self, capsys) -> None:
        """The namespace read is the one wrapped in an ``except KubectlExecutionError``, so it
        is where the two failure kinds meet, and an unreachable cluster has to come out with
        kubectl's own words: "did not answer which namespaces OPI created" sends an operator
        to labels and RBAC on a cluster that is not there. Measured with that except widened
        to KubectlConnectionError as well: 0 red over this file, test_argocd_stuck_delete and
        test_kubectl_connector, because the exit code is 2 either way."""
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": []})
        kubectl.get_namespace_label_map = AsyncMock(
            side_effect=KubectlConnectionError("kubectl connection failed: connection refused")
        )
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main([]) == 2

        printed = capsys.readouterr()
        assert "connection refused" in printed.err
        assert "namespaces OPI created" not in printed.err

    def test_an_unreadable_namespace_list_is_not_a_clean_cluster(self, capsys) -> None:
        """Without ``--namespace`` that map IS the work list. Measured with the refusal
        replaced by ``label_map = {}``: the sweep prints SCHOON and exits 0 over a cluster it
        read nothing in."""
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": []})
        kubectl.get_namespace_label_map = AsyncMock(side_effect=KubectlExecutionError("Failed to list namespaces: x"))
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main([]) == 2

        printed = capsys.readouterr()
        assert CLEAN not in printed.out
        assert "namespaces OPI created" in printed.err
        kubectl.list_tracked_resources.assert_not_awaited()


class TestTheCommandLine:
    """What the flags promise. The default is a REPORT: this tool deletes cluster
    resources, so nothing may go until it is asked for it in so many words.

    The exit code is the other half. The plan makes this sweep the last step of the
    delete test ("die hoort SCHOON te melden. Meldt hij iets, dan heeft de cascade niet
    alles meegenomen"), and a step that always exits 0 measures nothing.
    """

    @staticmethod
    def _cluster_with_one_orphan() -> AsyncMock:
        return _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": [_resource("mpfm-w3h-pr-310", "db-creds")]})

    def test_without_the_delete_flag_nothing_is_deleted(self) -> None:
        kubectl = self._cluster_with_one_orphan()
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            main(["--namespace", "rig-prd-mpfm-w3h"])

        kubectl.delete_tracked_resources.assert_not_awaited()

    def test_without_the_delete_flag_an_orphaned_directory_stays(self, tmp_path: Path) -> None:
        (tmp_path / "odcn-production/mpfm-w3h/pr-9").mkdir(parents=True)
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            main(["--namespace", "rig-prd-mpfm-w3h", "--deployments-repo", str(tmp_path)])

        assert (tmp_path / "odcn-production/mpfm-w3h/pr-9").is_dir()

    def test_the_delete_flag_is_what_deletes(self) -> None:
        kubectl = self._cluster_with_one_orphan()
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            main(["--namespace", "rig-prd-mpfm-w3h", "--delete"])

        deleted = kubectl.delete_tracked_resources.await_args.args[0]
        assert [r.name for r in deleted] == ["db-creds"]

    def test_finding_nothing_exits_clean(self) -> None:
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h"]) == 0

    def test_finding_an_orphan_exits_one(self) -> None:
        kubectl = self._cluster_with_one_orphan()
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h"]) == 1

    def test_finding_only_an_orphaned_path_exits_one_too(self, tmp_path: Path) -> None:
        (tmp_path / "odcn-production/mpfm-w3h/pr-9").mkdir(parents=True)
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            code = main(
                ["--namespace", "rig-prd-mpfm-w3h", "--deployments-repo", str(tmp_path), "--cluster", "odcn-production"]
            )

        assert code == 1

    def test_a_deletion_that_took_everything_exits_clean(self) -> None:
        kubectl = self._cluster_with_one_orphan()
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h", "--delete"]) == 0

    def test_a_resource_that_would_not_go_keeps_the_exit_code_at_one(self) -> None:
        """Deleting is not the same as gone: what is still standing must still be an exit 1."""
        kubectl = self._cluster_with_one_orphan()
        kubectl.delete_tracked_resources = AsyncMock(return_value=[_resource("mpfm-w3h-pr-310", "db-creds")])
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h", "--delete"]) == 1

    @staticmethod
    def _cluster_with_one_undecidable() -> AsyncMock:
        """A live Application whose name is longer than a label value holds, and a resource
        marked with exactly the first 63 characters of it."""
        return _kubectl(
            [{"metadata": {"name": LONG_APP}, "spec": {"source": {"path": "p"}}}],
            {"rig-prd-mpfm-w3h": [_resource(CUT_NAME, "db-creds", from_label=True)]},
        )

    def test_what_it_could_not_place_is_never_deleted(self) -> None:
        """Half of those resources belong to a running deployment, and --delete cannot ask."""
        kubectl = self._cluster_with_one_undecidable()
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            main(["--namespace", "rig-prd-mpfm-w3h", "--delete"])

        kubectl.delete_tracked_resources.assert_not_awaited()

    def test_what_it_could_not_place_is_not_a_clean_cluster(self) -> None:
        """SCHOON plus exit 0 is the all-clear a delete test reads. A resource nobody could
        place is exactly the case where that all-clear would be a guess."""
        kubectl = self._cluster_with_one_undecidable()
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h"]) == 1

    @staticmethod
    def _cluster_with_an_orphan_and_an_undecidable() -> AsyncMock:
        """Both at once, which is what makes the delete run at all.

        With only an undecidable there is nothing to delete, so ``remove()`` is never
        reached and every promise about what it is handed passes by default.
        """
        return _kubectl(
            [{"metadata": {"name": LONG_APP}, "spec": {"source": {"path": "p"}}}],
            {
                "rig-prd-mpfm-w3h": [
                    _resource("mpfm-w3h-pr-310", "db-creds"),
                    _resource(CUT_NAME, "maybe-the-neighbours", from_label=True),
                ]
            },
        )

    def test_a_delete_that_does_run_hands_over_only_the_orphans(self) -> None:
        """The undecidable one goes along the moment it is in the same list, and half of
        those belong to a running deployment."""
        kubectl = self._cluster_with_an_orphan_and_an_undecidable()
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            main(["--namespace", "rig-prd-mpfm-w3h", "--delete"])

        deleted = kubectl.delete_tracked_resources.await_args.args[0]
        assert [r.name for r in deleted] == ["db-creds"]

    def test_after_the_delete_what_could_not_be_placed_is_still_reported(self, capsys) -> None:
        """The second report is the one a delete test reads. Leaving it out there says the
        cluster is empty while the resource nobody could place is still standing."""
        kubectl = self._cluster_with_an_orphan_and_an_undecidable()
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h", "--delete"]) == 1

        out = capsys.readouterr().out
        # Anchored on the index, not on a split: absent, a split falls back to the whole
        # output and the FIRST report answers for the second.
        assert "Still standing" in out, out
        still_standing = out[out.index("Still standing") :]
        assert f"1 resource(s) whose mark sits on the {LABEL_VALUE_MAX}-character label cap" in still_standing
        assert f"{CUT_NAME}  rig-prd-mpfm-w3h/secret/maybe-the-neighbours" in still_standing

    def test_a_deployments_repo_that_is_not_a_directory_is_refused(self, tmp_path: Path) -> None:
        """Not 1: nothing was measured, so the git side cannot be called clean either."""
        not_a_repo = tmp_path / "zad-deployments"
        not_a_repo.write_text("")
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h", "--deployments-repo", str(not_a_repo)]) == 2

        kubectl.list_argocd_applications.assert_not_awaited()

    def test_the_clean_report_reaches_the_screen(self, capsys) -> None:
        """SCHOON is what the delete test reads; printing it is part of the contract."""
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            main(["--namespace", "rig-prd-mpfm-w3h"])

        assert capsys.readouterr().out.strip() == CLEAN


class TestRemove:
    @pytest.mark.asyncio
    async def test_reports_what_would_not_go(self) -> None:
        stubborn = _resource("app-a", "a")
        kubectl = _kubectl([], {})
        kubectl.delete_tracked_resources = AsyncMock(return_value=[stubborn])
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            remaining, _ = await remove([stubborn], [], None)

        assert remaining == [stubborn]

    @pytest.mark.asyncio
    async def test_removes_the_orphaned_directory_and_leaves_the_rest(self, tmp_path: Path) -> None:
        (tmp_path / "odcn-production/mpfm-w3h/pr-9").mkdir(parents=True)
        (tmp_path / "odcn-production/mpfm-w3h/pr-310").mkdir(parents=True)
        kubectl = _kubectl([], {})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            await remove([], ["odcn-production/mpfm-w3h/pr-9"], tmp_path)

        assert not (tmp_path / "odcn-production/mpfm-w3h/pr-9").exists()
        assert (tmp_path / "odcn-production/mpfm-w3h/pr-310").exists()


CLUSTER = "odcn-production"


def _project(repo: Path, name: str, deployments: list[dict], schema_version: float = 2.9) -> None:
    (repo / "projects").mkdir(parents=True, exist_ok=True)
    data = {"schema-version": schema_version, "name": name, "deployments": deployments}
    (repo / "projects" / f"{name}.yaml").write_text(yaml.safe_dump(data))


def _application(repo: Path, project: str, name: str, source: dict | None) -> None:
    folder = repo / CLUSTER / project
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": name}, "spec": {}}
    if source is not None:
        doc["spec"]["source"] = source
    (folder / f"{name}-argocd-application.yaml").write_text(yaml.safe_dump(doc))


class TestOrphanedApplications:
    """The argo-repo layer, measured on odcn-production on 9 October 2026: three
    Applications (dd-mco-pr-41, pm-5sj-pr139, toets-hn7-pr-32) kept running deployments
    the projects repo no longer had. ArgoCD was right to keep them: they stand in git."""

    def test_an_application_for_a_removed_deployment_is_reported(self, tmp_path: Path) -> None:
        _project(tmp_path / "p", "dd-mco", [{"name": "productie", "cluster": CLUSTER}])
        _application(tmp_path / "a", "dd-mco", "dd-mco-productie", {"path": f"./{CLUSTER}/dd-mco/productie"})
        _application(tmp_path / "a", "dd-mco", "dd-mco-pr-41", {"path": f"./{CLUSTER}/dd-mco/pr-41"})

        orphans = orphaned_applications(tmp_path / "a", CLUSTER, expected_render_roots(tmp_path / "p", CLUSTER))

        assert orphans == [("dd-mco-pr-41", f"{CLUSTER}/dd-mco/pr-41")]

    def test_the_project_level_and_infrastructure_roots_are_expected(self, tmp_path: Path) -> None:
        """Measured: algor-odc and mb-docs-helmfile point an Application at ``infrastructure``,
        every project at ``_project``. Neither is a deployment, both are the platform's own."""
        _project(tmp_path / "p", "algor-odc", [])
        _application(tmp_path / "a", "algor-odc", "algor-odc-project", {"path": f"./{CLUSTER}/algor-odc/_project"})
        _application(
            tmp_path / "a", "algor-odc", "algor-odc-infrastructure", {"path": f"{CLUSTER}/algor-odc/infrastructure"}
        )

        assert orphaned_applications(tmp_path / "a", CLUSTER, expected_render_roots(tmp_path / "p", CLUSTER)) == []

    def test_a_deployment_on_another_cluster_does_not_protect_this_one(self, tmp_path: Path) -> None:
        _project(tmp_path / "p", "wies", [{"name": "pr-9", "cluster": "local"}])
        _application(tmp_path / "a", "wies", "wies-pr-9", {"path": f"{CLUSTER}/wies/pr-9"})

        orphans = orphaned_applications(tmp_path / "a", CLUSTER, expected_render_roots(tmp_path / "p", CLUSTER))

        assert [name for name, _ in orphans] == ["wies-pr-9"]

    def test_an_older_schema_is_read_after_migration(self, tmp_path: Path) -> None:
        """The repo holds every version from 2 to the latest."""
        _project(tmp_path / "p", "wies", [{"name": "pr-9", "cluster": CLUSTER}], schema_version=2)

        assert f"{CLUSTER}/wies/pr-9" in expected_render_roots(tmp_path / "p", CLUSTER)

    def test_an_application_with_several_sources_is_not_judged(self, tmp_path: Path) -> None:
        _project(tmp_path / "p", "wies", [])
        _application(tmp_path / "a", "wies", "wies-multi", None)

        assert orphaned_applications(tmp_path / "a", CLUSTER, expected_render_roots(tmp_path / "p", CLUSTER)) == []

    def test_without_project_files_it_refuses(self, tmp_path: Path) -> None:
        """An empty projects checkout expects nothing, so every Application would be reported."""
        (tmp_path / "p" / "projects").mkdir(parents=True)
        with pytest.raises(SweepRefused, match="no project files"):
            expected_render_roots(tmp_path / "p", CLUSTER)

    def test_a_project_file_that_does_not_parse_refuses(self, tmp_path: Path) -> None:
        (tmp_path / "p" / "projects").mkdir(parents=True)
        (tmp_path / "p" / "projects" / "kapot.yaml").write_text("name: [unclosed\n")
        with pytest.raises(SweepRefused, match="does not parse"):
            expected_render_roots(tmp_path / "p", CLUSTER)


class TestRenderedKinds:
    @staticmethod
    def _write(tmp_path: Path, text: str, name: str = "a.yaml") -> Path:
        folder = tmp_path / CLUSTER / "wies" / "pr-9"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)
        return tmp_path

    def test_reads_kind_and_group_from_every_document(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: a}\n---\napiVersion: v1\nkind: Service\n",
        )

        assert rendered_kinds(tmp_path, CLUSTER) == ({"Deployment.apps", "Service"}, [])

    def test_a_nested_kind_is_not_a_rendered_one(self, tmp_path: Path) -> None:
        """The ``kind:`` of a roleRef names what is referred to, not what is created."""
        self._write(
            tmp_path,
            "apiVersion: rbac.authorization.k8s.io/v1\nkind: RoleBinding\nroleRef:\n  kind: ClusterRole\n",
        )

        assert rendered_kinds(tmp_path, CLUSTER) == ({"RoleBinding.rbac.authorization.k8s.io"}, [])

    def test_an_encrypted_kind_counts_as_a_secret(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "apiVersion: ENC[AES256_GCM,data:L4w=,type:str]\nkind: ENC[AES256_GCM,data:fOOjCCXY,type:str]\n",
            name="x-secret.sops.yaml",
        )

        assert rendered_kinds(tmp_path, CLUSTER) == ({"Secret"}, [])

    def test_a_helm_template_is_read_without_being_parsed(self, tmp_path: Path) -> None:
        """Measured: 1401 files in the helmfile projects are chart templates, not YAML,
        and ~600 of them template their apiVersion. The kind is still literal there."""
        self._write(
            tmp_path,
            "{{- if .Values.enabled }}\n"
            'apiVersion: {{ include "common.capabilities.ingress.apiVersion" . }}\n'
            "kind: Ingress\n{{- end }}\n",
        )

        assert rendered_kinds(tmp_path, CLUSTER) == ({"Ingress.*"}, [])

    def test_a_templated_kind_is_reported_not_guessed(self, tmp_path: Path) -> None:
        self._write(tmp_path, "apiVersion: apps/v1\nkind: {{ .Values.master.kind }}\n", name="master.yaml")

        assert rendered_kinds(tmp_path, CLUSTER) == (set(), [f"{CLUSTER}/wies/pr-9/master.yaml"])

    def test_only_this_clusters_folder_is_read(self, tmp_path: Path) -> None:
        (tmp_path / "local" / "wies" / "pr-9").mkdir(parents=True)
        (tmp_path / "local" / "wies" / "pr-9" / "a.yaml").write_text("apiVersion: v1\nkind: ConfigMap\n")
        (tmp_path / CLUSTER).mkdir()

        assert rendered_kinds(tmp_path, CLUSTER) == (set(), [])


class TestRenderedTypes:
    SERVED: ClassVar[dict[str, str]] = {
        "Ingress.networking.k8s.io": "ingresses.networking.k8s.io",
        "Ingress.extensions": "ingresses.extensions",
        "Secret": "secrets",
    }

    def test_a_wildcard_group_takes_every_group_serving_the_kind(self) -> None:
        assert rendered_types({"Ingress.*"}, self.SERVED) == ["ingresses.extensions", "ingresses.networking.k8s.io"]

    def test_a_kind_the_cluster_does_not_serve_is_left_out(self) -> None:
        """It cannot have been created there; asking for it would only fail the query."""
        assert rendered_types({"Secret", "PodSecurityPolicy.policy"}, self.SERVED) == ["secrets"]


class TestCompletenessInATenant:
    """Measured on odcn-production, 9 October 2026: the tenant account may not list ~90
    of the types the cluster serves, so every namespace answered for part of its types
    and the sweep refused the whole cluster on the first one. What decides there is
    whether every kind the deployments repo renders answered."""

    @staticmethod
    def _repo(tmp_path: Path) -> Path:
        folder = tmp_path / CLUSTER / "mpfb-8wh" / "pr-303"
        folder.mkdir(parents=True)
        (folder / "a.yaml").write_text("apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: a}\n")
        return tmp_path

    @staticmethod
    def _tenant(rendered_answer: tuple[list[TrackedResource], bool] | None) -> AsyncMock:
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfb-8wh": []})
        kubectl.list_namespaced_kinds = AsyncMock(
            return_value={"Deployment.apps": "deployments.apps", "Prometheus.monitoring.coreos.com": "prometheuses"}
        )
        partial = ([_resource("mpfb-8wh-pr-303", "creds", namespace="rig-prd-mpfb-8wh")], False)

        async def answer(_ns: str, types: list[str]) -> tuple[list[TrackedResource], bool] | None:
            # An empty type list is None at the connector, not an answer (kubectl.py).
            if not types:
                return None
            return rendered_answer if types == ["deployments.apps"] else partial

        kubectl.list_tracked_resources = AsyncMock(side_effect=answer)
        return kubectl

    @pytest.mark.asyncio
    async def test_a_partial_answer_counts_when_every_rendered_kind_answered(self, tmp_path: Path) -> None:
        deployment = _resource("mpfb-8wh-pr-303", "uitvraag", kind="Deployment", namespace="rig-prd-mpfb-8wh")
        kubectl = self._tenant(([deployment], True))
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            resources, _, _ = await inventory(["rig-prd-mpfb-8wh"], self._repo(tmp_path), CLUSTER)

        assert sorted(r.name for r in resources) == ["creds", "uitvraag"]
        assert kubectl.list_tracked_resources.await_args_list[1].args == ("rig-prd-mpfb-8wh", ["deployments.apps"])

    @pytest.mark.asyncio
    async def test_a_rendered_kind_that_does_not_answer_refuses(self, tmp_path: Path) -> None:
        kubectl = self._tenant(([], False))
        with (
            patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl),
            pytest.raises(SweepRefused, match="every kind the deployments repo renders"),
        ):
            await inventory(["rig-prd-mpfb-8wh"], self._repo(tmp_path), CLUSTER)

    @pytest.mark.asyncio
    async def test_without_the_repo_a_partial_answer_still_refuses(self) -> None:
        kubectl = self._tenant(([], True))
        with (
            patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl),
            pytest.raises(SweepRefused, match="pass --deployments-repo"),
        ):
            await inventory(["rig-prd-mpfb-8wh"], None, CLUSTER)

    @pytest.mark.asyncio
    async def test_a_repo_that_renders_nothing_for_this_cluster_refuses(self, tmp_path: Path) -> None:
        """A stale checkout, or the wrong --cluster, renders no kinds at all. Judging a
        partial namespace over an empty set would call every one of them measured."""
        (tmp_path / "local" / "wies" / "pr-9").mkdir(parents=True)
        kubectl = self._tenant(([], True))
        with (
            patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl),
            pytest.raises(SweepRefused, match="every kind the deployments repo renders"),
        ):
            await inventory(["rig-prd-mpfb-8wh"], tmp_path, CLUSTER)

    @pytest.mark.asyncio
    async def test_a_failed_kind_discovery_refuses(self, tmp_path: Path) -> None:
        kubectl = self._tenant(([], True))
        kubectl.list_namespaced_kinds = AsyncMock(return_value=None)
        with (
            patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl),
            pytest.raises(SweepRefused, match="which kinds it serves"),
        ):
            await inventory(["rig-prd-mpfb-8wh"], self._repo(tmp_path), CLUSTER)


class TestHelmfileProjectsAreLeftAlone:
    """mb-docs-helmfile and mb-grist-helmfile render their manifests from charts OPI passes
    through. What is theirs is not this tool's to judge, in none of the three layers."""

    HELMFILE: ClassVar[list[dict]] = [{"name": "production", "cluster": CLUSTER, "helmfile": [{"name": "docs"}]}]

    def test_a_project_with_a_helmfile_deployment_is_one(self, tmp_path: Path) -> None:
        _project(tmp_path, "mb-docs-helmfile", self.HELMFILE)
        _project(tmp_path, "wies", [{"name": "main", "cluster": CLUSTER}])

        assert helmfile_projects(tmp_path) == {"mb-docs-helmfile"}

    def test_its_namespaces_include_the_infrastructure_one(self, tmp_path: Path) -> None:
        _project(tmp_path, "mb-docs-helmfile", self.HELMFILE)

        assert helmfile_namespaces(tmp_path, CLUSTER) == {
            "rig-prd-mb-docs-helmfile",
            "rig-prd-mb-docs-helmfile-infrastructure",
        }

    def test_its_applications_are_not_judged(self, tmp_path: Path) -> None:
        _project(tmp_path / "p", "mb-docs-helmfile", self.HELMFILE)
        _application(
            tmp_path / "a", "mb-docs-helmfile", "mb-docs-helmfile-x", {"path": f"{CLUSTER}/mb-docs-helmfile/x"}
        )

        expected = expected_render_roots(tmp_path / "p", CLUSTER)
        assert orphaned_applications(tmp_path / "a", CLUSTER, expected, frozenset({"mb-docs-helmfile"})) == []

    def test_its_manifests_are_not_read_for_kinds(self, tmp_path: Path) -> None:
        folder = tmp_path / CLUSTER / "mb-docs-helmfile" / "production"
        folder.mkdir(parents=True)
        (folder / "a.yaml").write_text("apiVersion: gateway.networking.k8s.io/v1\nkind: HTTPRoute\n")

        assert rendered_kinds(tmp_path, CLUSTER, frozenset({"mb-docs-helmfile"})) == (set(), [])

    @pytest.mark.asyncio
    async def test_its_namespaces_and_paths_are_not_swept(self, tmp_path: Path) -> None:
        (tmp_path / CLUSTER / "mb-docs-helmfile" / "old").mkdir(parents=True)
        kubectl = _kubectl(
            _LIVE_APPLICATIONS,
            {
                "rig-prd-mb-docs-helmfile": [_resource("gone", "x", namespace="rig-prd-mb-docs-helmfile")],
                "rig-prd-wies": [],
            },
        )
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            resources, _, paths = await inventory(
                None,
                tmp_path,
                None,
                frozenset({"mb-docs-helmfile"}),
                frozenset({"rig-prd-mb-docs-helmfile"}),
            )

        assert resources == []
        assert paths == []
        assert [call.args[0] for call in kubectl.list_tracked_resources.await_args_list] == ["rig-prd-wies"]

    @pytest.mark.asyncio
    async def test_naming_its_namespace_is_refused(self) -> None:
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mb-docs-helmfile": []})
        with (
            patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl),
            pytest.raises(SweepRefused, match="helmfile"),
        ):
            await inventory(
                ["rig-prd-mb-docs-helmfile"], None, None, frozenset(), frozenset({"rig-prd-mb-docs-helmfile"})
            )


class TestOtherClustersPaths:
    @pytest.mark.asyncio
    async def test_another_clusters_folder_is_not_this_ones_orphan(self, tmp_path: Path) -> None:
        """The Applications are read from ONE cluster, so another cluster's render roots
        have nothing pointing at them here. ``--delete`` would remove the local/ tree."""
        (tmp_path / "local/wies/staging").mkdir(parents=True)
        (tmp_path / f"{CLUSTER}/wies/pr-9").mkdir(parents=True)
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-wies": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            *_, paths = await inventory(["rig-prd-wies"], tmp_path, CLUSTER)

        assert paths == [f"{CLUSTER}/wies/pr-9"]


class TestWhatTheProjectsRepoAsksFor:
    """A directory is not an orphan just because no Application points at it RIGHT NOW.

    An Application the cluster has not got yet, or one a force just removed, leaves its
    deployment standing in the projects repo. The argo layer then calls that Application
    legitimate while this layer called its directory an orphan, and ``--delete`` removed it
    and exited 0: measured on this branch before the roots protected it.
    """

    @pytest.mark.asyncio
    async def test_a_root_the_projects_repo_asks_for_is_not_an_orphan(self, tmp_path: Path) -> None:
        (tmp_path / CLUSTER / "dd-mco" / "productie").mkdir(parents=True)
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-dd-mco": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            *_, paths = await inventory(
                None, tmp_path, CLUSTER, expected_roots=frozenset({f"{CLUSTER}/dd-mco/productie"})
            )

        assert paths == []

    @pytest.mark.asyncio
    async def test_a_root_it_does_not_ask_for_is_still_an_orphan(self, tmp_path: Path) -> None:
        (tmp_path / CLUSTER / "dd-mco" / "pr-41").mkdir(parents=True)
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-dd-mco": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            *_, paths = await inventory(
                None, tmp_path, CLUSTER, expected_roots=frozenset({f"{CLUSTER}/dd-mco/productie"})
            )

        assert paths == [f"{CLUSTER}/dd-mco/pr-41"]

    def test_delete_leaves_the_directory_of_a_live_deployment_standing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The whole chain: projects repo, argo repo, --delete. The Application is NOT on
        the cluster, which is exactly the state a forced deletion leaves behind."""
        _project(tmp_path / "p", "dd-mco", [{"name": "productie", "cluster": CLUSTER}])
        _application(tmp_path / "a", "dd-mco", "dd-mco-productie", {"path": f"{CLUSTER}/dd-mco/productie"})
        directory = tmp_path / "d" / CLUSTER / "dd-mco" / "productie"
        directory.mkdir(parents=True)
        (directory / "s.yaml").write_text("apiVersion: v1\nkind: Secret\nmetadata: {name: s}\n")
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-dd-mco": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            code = main(
                [
                    "--cluster",
                    CLUSTER,
                    "--projects-repo",
                    str(tmp_path / "p"),
                    "--argo-repo",
                    str(tmp_path / "a"),
                    "--deployments-repo",
                    str(tmp_path / "d"),
                    "--delete",
                ]
            )

        assert (code, directory.exists()) == (0, True)
        assert capsys.readouterr().out.strip() == CLEAN


class TestTheArgoLayerOnTheCommandLine:
    def test_an_application_git_still_keeps_is_reported_and_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _project(tmp_path / "p", "dd-mco", [])
        _application(tmp_path / "a", "dd-mco", "dd-mco-pr-41", {"path": f"{CLUSTER}/dd-mco/pr-41"})
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-dd-mco": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            code = main(
                ["--cluster", CLUSTER, "--projects-repo", str(tmp_path / "p"), "--argo-repo", str(tmp_path / "a")]
            )

        assert code == 1
        assert "dd-mco-pr-41" in capsys.readouterr().out

    def test_delete_leaves_the_argo_repo_alone(self, tmp_path: Path) -> None:
        """Removing an Application is a commit, and the argo repo is OPI's to write."""
        _project(tmp_path / "p", "dd-mco", [])
        _application(tmp_path / "a", "dd-mco", "dd-mco-pr-41", {"path": f"{CLUSTER}/dd-mco/pr-41"})
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-dd-mco": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            main(
                [
                    "--cluster",
                    CLUSTER,
                    "--projects-repo",
                    str(tmp_path / "p"),
                    "--argo-repo",
                    str(tmp_path / "a"),
                    "--delete",
                ]
            )

        assert (tmp_path / "a" / CLUSTER / "dd-mco" / "dd-mco-pr-41-argocd-application.yaml").exists()

    def test_deleting_directories_without_the_projects_repo_is_refused(self, tmp_path: Path) -> None:
        """Exit 2 alone does not pin this: main() answers 2 for an unreachable cluster too,
        so without the factory patched the refusal reads as green on a runner without one."""
        (tmp_path / "d").mkdir()
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector") as factory:
            assert main(["--deployments-repo", str(tmp_path / "d"), "--delete"]) == 2

        factory.assert_not_called()

    def test_deleting_directories_without_a_cluster_is_refused(self, tmp_path: Path) -> None:
        """The protection is computed only when the cluster is named too, so all three repos
        with an empty --cluster deleted the directory of a live deployment and said SCHOON.
        An empty CLUSTER_MANAGER in the environment is enough to get here without typing it."""
        for name in ("d", "p", "a"):
            (tmp_path / name).mkdir()
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector") as factory:
            assert (
                main(
                    [
                        "--deployments-repo",
                        str(tmp_path / "d"),
                        "--projects-repo",
                        str(tmp_path / "p"),
                        "--argo-repo",
                        str(tmp_path / "a"),
                        "--cluster",
                        "",
                        "--delete",
                    ]
                )
                == 2
            )

        factory.assert_not_called()

    def test_one_repo_without_the_other_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "p").mkdir()
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector") as factory:
            assert main(["--projects-repo", str(tmp_path / "p")]) == 2

        factory.assert_not_called()


class TestTheTask:
    """``task argocd-orphan-sweep`` is the documented way in (scripts/README.md and
    features/argocd-vastgelopen-verwijdering.md), so the flags it hands the script are
    part of the contract, and nothing measured them.

    The one that matters is DELETE. go-task's bare ``{{if .DELETE}}`` is true for EVERY
    non-empty value, and that is what this task had, so ``DELETE=0`` deleted. On a flag
    that removes cluster resources, anything but the word asked for has to do nothing.
    """

    @staticmethod
    def _task(name: str) -> dict:
        return yaml.safe_load(TASKFILE.read_text())["tasks"][name]

    def _cmd(self) -> str:
        return "\n".join(str(cmd) for cmd in self._task(SWEEP_TASK)["cmds"])

    def test_the_task_runs_the_sweep_script(self) -> None:
        assert "scripts/argocd_orphan_sweep.py" in self._cmd()
        assert self._task(SWEEP_TASK)["dir"] == "operations-manager/python"

    def test_the_delete_flag_is_not_wired_on_bare_truthiness(self) -> None:
        """The guard that runs everywhere; the rendered proof below needs a go-task binary."""
        cmd = self._cmd()

        assert "--delete" in cmd
        assert "{{if .DELETE}}" not in cmd
        assert '{{if eq (default "0" .DELETE) "1"}}' in cmd


@pytest.mark.skipif(shutil.which("task") is None, reason="requires the go-task binary")
class TestTheTaskAsRendered:
    """What go-task actually makes of those templates. ``--dry`` renders without running."""

    @staticmethod
    def _rendered(**env: str) -> str:
        # CLUSTER_TYPE staat in de standaard-env: de taak eist hem, want zonder gekozen
        # cluster gaf hij CLUSTER_MANAGER leeg mee. Een test over DELETE of de repo-paden
        # zegt niets over welk cluster het is.
        proc = subprocess.run(
            ["task", "--dry", "-v", SWEEP_TASK],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            env={
                "PATH": os.environ["PATH"],
                "HOME": os.environ.get("HOME", ""),
                "CLUSTER_TYPE": "sandboxed-local",
                **env,
            },
        )
        assert proc.returncode == 0, proc.stderr
        # go-task writes the command it would run to stderr, not to stdout.
        prefix = f"task: [{SWEEP_TASK}]"
        line = next(
            line for line in proc.stderr.splitlines() if line.startswith(prefix) and "argocd_orphan_sweep.py" in line
        )
        return line[len(prefix) :].strip()

    def test_without_delete_it_only_reports(self) -> None:
        assert "--delete" not in self._rendered()

    @pytest.mark.parametrize("value", ["0", "false"])
    def test_anything_but_one_only_reports(self, value: str) -> None:
        """``DELETE=0`` is the form this task got wrong; ``false`` is the same intent
        spelled differently. On a destructive flag, a value that is not the one asked
        for has to do nothing rather than something."""
        assert "--delete" not in self._rendered(DELETE=value)

    def test_delete_one_is_what_deletes(self) -> None:
        assert "--delete" in self._rendered(DELETE="1")

    def test_the_namespace_and_repo_variables_reach_the_script(self) -> None:
        rendered = self._rendered(NAMESPACE="rig-prd-mpfm-w3h", REPO="/checkouts/zad deployments")

        assert "--namespace rig-prd-mpfm-w3h" in rendered
        assert '--deployments-repo "/checkouts/zad deployments"' in rendered

    def test_without_variables_the_whole_cluster_is_swept(self) -> None:
        rendered = self._rendered()

        assert "--namespace" not in rendered
        assert "--deployments-repo" not in rendered

    def test_gitops_hands_over_all_three_repos(self) -> None:
        rendered = self._rendered(GITOPS="/checkouts")

        assert '--projects-repo "/checkouts/rig-cluster-projects-github"' in rendered
        assert '--argo-repo "/checkouts/argo-applications-github"' in rendered
        assert '--deployments-repo "/checkouts/rig-cluster-application-test-github"' in rendered

    def test_the_selected_cluster_reaches_the_script(self) -> None:
        """Measured 9 October 2026: with select-cluster on production the script still read
        ``ARGOCD_MANAGER=rig-system`` from the .env of operations-manager/python, looked
        for Applications in the sandbox's namespace, and refused."""
        rendered = self._rendered(CLUSTER_TYPE="odcn-production", RIG_NAMESPACE="rig-prd-operations")

        assert rendered.startswith("ARGOCD_MANAGER=rig-prd-operations CLUSTER_MANAGER=odcn-production ")
