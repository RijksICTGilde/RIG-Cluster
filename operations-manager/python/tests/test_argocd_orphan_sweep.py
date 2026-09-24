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

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from opi.utils.argocd_tracking import TrackedResource
from scripts.argocd_orphan_sweep import (
    CLEAN,
    SweepRefused,
    format_report,
    inventory,
    main,
    orphaned_paths,
    orphaned_resources,
    remove,
    render_roots,
)

if TYPE_CHECKING:
    from pathlib import Path


def _resource(app_name: str, name: str, kind: str = "Secret", namespace: str = "rig-prd-mpfm-w3h") -> TrackedResource:
    return TrackedResource(
        kind=kind,
        api_version="v1",
        name=name,
        namespace=namespace,
        app_name=app_name,
        tracking_id=f"{app_name}:/{kind}:{namespace}/{name}",
        being_deleted=False,
    )


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

    def test_a_referenced_path_is_not_an_orphan(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path, "odcn-production/mpfm-w3h/pr-310")
        assert orphaned_paths(repo, {"odcn-production/mpfm-w3h/pr-310"}) == []

    def test_an_unreferenced_path_is_an_orphan(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path, "odcn-production/mpfm-w3h/pr-310", "odcn-production/mpfm-w3h/pr-9")
        assert orphaned_paths(repo, {"odcn-production/mpfm-w3h/pr-310"}) == ["odcn-production/mpfm-w3h/pr-9"]

    def test_a_reference_with_slashes_around_it_still_protects(self, tmp_path: Path) -> None:
        """spec.source.path is written both bare and with a leading slash; both mean the same path."""
        repo = self._repo(tmp_path, "odcn-production/mpfm-w3h/pr-310")
        assert orphaned_paths(repo, {"/odcn-production/mpfm-w3h/pr-310/"}) == []


class TestReport:
    def test_nothing_found_is_reported_as_clean(self) -> None:
        """The last step of a delete test: it measures what is left, not what seemed to happen."""
        assert format_report([], []) == CLEAN

    def test_resources_are_grouped_under_their_application(self) -> None:
        report = format_report([_resource("app-a", "a"), _resource("app-b", "b"), _resource("app-a", "c")], [])
        assert "Found 3 resource(s)" in report
        assert "app-a  (2 resources in rig-prd-mpfm-w3h)" in report
        assert "app-b  (1 resources in rig-prd-mpfm-w3h)" in report

    def test_paths_are_reported_too(self) -> None:
        report = format_report([], ["odcn-production/mpfm-w3h/pr-9"])
        assert "1 deployments-repo path(s)" in report
        assert "odcn-production/mpfm-w3h/pr-9" in report

    def test_after_a_delete_the_report_says_what_is_still_standing(self) -> None:
        report = format_report([_resource("app-a", "a")], [], after_delete=True)
        assert report.startswith("Still standing 1 resource(s)")


#: One Application the cluster still runs. An empty list is a refusal, see TestRefusal.
_LIVE_APPLICATIONS = [{"metadata": {"name": "user-applications"}, "spec": {"source": {"path": "root"}}}]


def _kubectl(applications: list[dict], tracked: dict[str, list[TrackedResource]]) -> AsyncMock:
    kubectl = AsyncMock()
    kubectl.list_argocd_applications = AsyncMock(return_value=applications)
    kubectl.get_namespace_label_map = AsyncMock(return_value=dict.fromkeys(tracked, ""))
    kubectl.list_namespaced_resource_types = AsyncMock(return_value=["secrets"])
    kubectl.list_tracked_resources = AsyncMock(side_effect=lambda ns, _types: tracked.get(ns, []))
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
            resources, paths = await inventory(None, None)

        assert [r.name for r in resources] == ["db-creds"]
        assert paths == []
        assert [call.args[0] for call in kubectl.list_tracked_resources.await_args_list] == [
            "rig-prd-mpfm-w3h",
            "rig-prd-other",
        ]

    @pytest.mark.asyncio
    async def test_the_kube_system_namespaces_are_left_alone(self) -> None:
        """What carries a tracking-id there comes from the bootstrap, not from a user Application."""
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"kube-system": [_resource("something", "x")], "rig-prd-mpfm-w3h": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            resources, _ = await inventory(None, None)

        assert resources == []
        assert [call.args[0] for call in kubectl.list_tracked_resources.await_args_list] == ["rig-prd-mpfm-w3h"]

    @pytest.mark.asyncio
    async def test_a_named_namespace_is_the_only_one_queried(self) -> None:
        kubectl = _kubectl(_LIVE_APPLICATIONS, {"rig-prd-mpfm-w3h": [], "rig-prd-other": []})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            await inventory(["rig-prd-mpfm-w3h"], None)

        assert [call.args[0] for call in kubectl.list_tracked_resources.await_args_list] == ["rig-prd-mpfm-w3h"]
        kubectl.get_namespace_label_map.assert_not_awaited()

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
            _, paths = await inventory(["rig-prd-mpfm-w3h"], tmp_path)

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

    def test_the_cli_answers_a_refusal_with_its_own_exit_code(self) -> None:
        """Not 0 (clean) and not 1 (orphans found): nothing was measured."""
        kubectl = _kubectl([], {})
        with patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl):
            assert main(["--namespace", "rig-prd-mpfm-w3h"]) == 2


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
