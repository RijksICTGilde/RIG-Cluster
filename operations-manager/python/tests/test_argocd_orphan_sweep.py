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
from unittest.mock import AsyncMock, patch

import pytest
import yaml
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

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKFILE = REPO_ROOT / "Taskfile.yaml"
SWEEP_TASK = "argocd-orphan-sweep"


def _resource(app_name: str, name: str, kind: str = "Secret", namespace: str = "rig-prd-mpfm-w3h") -> TrackedResource:
    return TrackedResource(
        kind=kind,
        api_version="v1",
        name=name,
        namespace=namespace,
        app_name=app_name,
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

    def test_an_application_without_a_path_protects_nothing(self, tmp_path: Path) -> None:
        """An empty string normalises to ``.``; reading that as a render root would
        silently protect a directory named after nothing."""
        repo = self._repo(tmp_path, "odcn-production/mpfm-w3h/pr-310")
        assert orphaned_paths(repo, {""}) == ["odcn-production/mpfm-w3h/pr-310"]


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


def _kubectl(
    applications: list[dict],
    tracked: dict[str, list[TrackedResource]],
    namespace_labels: dict[str, str] | None = None,
) -> AsyncMock:
    kubectl = AsyncMock()
    kubectl.list_argocd_applications = AsyncMock(return_value=applications)
    kubectl.get_namespace_label_map = AsyncMock(
        return_value=namespace_labels if namespace_labels is not None else dict.fromkeys(tracked, "operations-manager")
    )
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
            resources, _ = await inventory(None, None)

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
            resources, _ = await inventory(None, None)

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
            _, paths = await inventory(["rig-prd-mpfm-w3h"], tmp_path)

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
        kubectl.list_tracked_resources = AsyncMock(side_effect=lambda ns, _types: None if ns == "rig-prd-other" else [])
        with (
            patch("scripts.argocd_orphan_sweep.create_kubectl_connector", return_value=kubectl),
            pytest.raises(SweepRefused, match="rig-prd-other"),
        ):
            await inventory(None, None)

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
            assert main(["--namespace", "rig-prd-mpfm-w3h", "--deployments-repo", str(tmp_path)]) == 1

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
        proc = subprocess.run(
            ["task", "--dry", "-v", SWEEP_TASK],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", ""), **env},
        )
        assert proc.returncode == 0, proc.stderr
        # go-task writes the command it would run to stderr, not to stdout.
        prefix = f"task: [{SWEEP_TASK}]"
        line = next(line for line in proc.stderr.splitlines() if line.startswith(prefix))
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
