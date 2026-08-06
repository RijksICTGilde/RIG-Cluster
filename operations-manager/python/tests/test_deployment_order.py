"""Clone sources must be provisioned before the deployments that clone from them.

Nothing enforced this: process_project walked the deployments in file order, which
happened to work only because the production files grew that way. A fresh environment
(the upgrade-safety sandbox) re-runs every clone, so there the order decides whether
provisioning succeeds.
"""

from __future__ import annotations

import pytest
from opi.services.deployment_order import clone_source_name, order_deployments_by_clone_dependency


def _dep(name: str, clone_from: str | None = None, clone_type: str = "deployment") -> dict:
    d: dict = {"name": name, "cluster": "odcn-production"}
    if clone_from:
        d["clone-from"] = {"type": clone_type, "reference": clone_from, "mode": "once"}
    return d


def _names(deployments: list[dict]) -> list[str]:
    return [d["name"] for d in deployments]


class TestCloneSourceName:
    def test_reads_a_deployment_reference(self):
        assert clone_source_name(_dep("pr-1", "main")) == "main"

    def test_no_clone_means_no_constraint(self):
        assert clone_source_name(_dep("main")) is None

    @pytest.mark.parametrize("clone_type", ["backup", "remote-source"])
    def test_sources_outside_the_project_impose_no_order(self, clone_type: str):
        """A backup or remote source is not a deployment here, so it cannot be ordered."""
        assert clone_source_name(_dep("pr-1", "somewhere", clone_type=clone_type)) is None


class TestOrdering:
    def test_a_source_listed_after_its_clone_is_moved_ahead(self):
        # The case that would break silently today: a deployment added at the top of the file.
        deployments = [_dep("pr-1", "main"), _dep("main")]
        assert _names(order_deployments_by_clone_dependency(deployments)) == ["main", "pr-1"]

    def test_order_is_untouched_when_it_is_already_correct(self):
        """regel-k4c's shape: the source first, four PR deployments cloning from it."""
        deployments = [_dep("regelrecht"), *(_dep(f"pr{i}", "regelrecht") for i in (933, 1037, 1045))]
        assert _names(order_deployments_by_clone_dependency(deployments)) == [
            "regelrecht",
            "pr933",
            "pr1037",
            "pr1045",
        ]

    def test_a_project_without_clones_comes_back_unchanged(self):
        deployments = [_dep("c"), _dep("a"), _dep("b")]
        assert _names(order_deployments_by_clone_dependency(deployments)) == ["c", "a", "b"]

    def test_unrelated_deployments_keep_their_file_order(self):
        """Stable: only what the clone graph demands moves, so diffs stay readable."""
        deployments = [_dep("z"), _dep("pr-1", "main"), _dep("y"), _dep("main"), _dep("x")]
        assert _names(order_deployments_by_clone_dependency(deployments)) == ["z", "main", "pr-1", "y", "x"]

    def test_a_chain_is_resolved_end_to_end(self):
        deployments = [_dep("c", "b"), _dep("b", "a"), _dep("a")]
        assert _names(order_deployments_by_clone_dependency(deployments)) == ["a", "b", "c"]

    def test_a_missing_source_is_left_alone(self):
        """wies has pr-274 cloning from 'staging', which no longer exists in the file.

        Treating that as an error would turn a historical leftover into a hard stop, which
        is the failure mode where a project silently stops deploying. Provisioning can tell
        "already cloned" from "cannot clone"; ordering cannot.
        """
        deployments = [_dep("pr-274", "staging"), _dep("production"), _dep("main")]
        assert _names(order_deployments_by_clone_dependency(deployments)) == ["pr-274", "production", "main"]

    def test_a_cycle_is_rejected_with_the_path_that_shows_it(self):
        deployments = [_dep("a", "b"), _dep("b", "a")]
        with pytest.raises(ValueError, match="Circular clone-from"):
            order_deployments_by_clone_dependency(deployments)

    def test_every_deployment_survives_the_reordering(self):
        deployments = [_dep("pr-1", "main"), _dep("main"), _dep("solo"), _dep("pr-2", "main")]
        assert sorted(_names(order_deployments_by_clone_dependency(deployments))) == sorted(_names(deployments))


class TestTheOrderingIsActuallyWiredIn:
    """The module existed with tests and was imported by nothing.

    That is the worst shape a fix can have: `order_deployments_by_clone_dependency` and
    its 85 lines of tests were added on 3 August, the commit never touched
    `project_manager.py`, and clones kept being processed in file order. Everything
    looked handled. These tests fail if the wiring disappears again.
    """

    def test_get_deployments_returns_a_clone_after_its_source(self) -> None:
        """Ordered in ``get_deployments`` and not at a call site: that is the one door
        every caller comes through, so a new caller is right without knowing about it."""
        import inspect

        from opi.manager.project_manager import ProjectManager

        source = inspect.getsource(ProjectManager.get_deployments)

        assert "order_deployments_by_clone_dependency" in source
        assert "return order_deployments_by_clone_dependency(deployments)" in source

    def test_the_module_is_imported_where_the_work_happens(self) -> None:
        import opi.manager.project_manager as pm

        assert hasattr(pm, "order_deployments_by_clone_dependency")

    def test_ordering_runs_after_the_filters(self) -> None:
        """Ordering the deployments you are going to use is the point. A source filtered
        away (another cluster, a single-deployment request) cannot be waited for anyway,
        so ordering before the filters would only reorder things that get dropped."""
        import inspect

        from opi.manager.project_manager import ProjectManager

        source = inspect.getsource(ProjectManager.get_deployments)

        assert source.index("cluster_filter") < source.index("order_deployments_by_clone_dependency")
        assert source.index("_resolve_deployment_filter") < source.index("order_deployments_by_clone_dependency")
