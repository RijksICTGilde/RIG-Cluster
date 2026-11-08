"""The unit layer must not start a kubectl process.

``KubectlConnector.__init__`` probes the cluster with a blocking ``kubectl auth whoami`` and
the singleton is reset around every test, so without the arrangement in
``tests/kubectl_probe.py`` every test that builds a connector starts that process, each a ten
second wait when the cluster is unreachable. These tests fail when that arrangement stops
working.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from opi.connectors.kubectl import KubectlConnector
from opi.manager.project_manager import ProjectManager
from tests.kubectl_probe import (
    ClusterCommandInUnitLayerError,
    measures_a_real_cluster,
    refusing_cluster_commands,
)


@dataclass
class _CollectedTest:
    """What ``measures_a_real_cluster`` reads from a collected test."""

    path: Path
    markers: list[str] = field(default_factory=list)

    def iter_markers(self) -> list[pytest.Mark]:
        return [pytest.Mark(name, (), {}) for name in self.markers]


def test_building_a_connector_starts_no_process() -> None:
    """The autouse fixture of this layer replaced the probe for the whole test."""
    assert KubectlConnector()._initialized is True
    assert KubectlConnector._retry_task is None


def test_building_a_project_manager_starts_no_process() -> None:
    """``ProjectManager`` builds a connector of its own, which is where the probe hid."""
    assert ProjectManager() is not None


def test_a_test_that_resets_the_singleton_itself_starts_no_process() -> None:
    """Several files reset the singleton in a fixture of their own, so the arrangement cannot
    be one prepared instance: ``__init__`` itself has to skip the probe."""
    KubectlConnector._instance = None
    connector = KubectlConnector()

    assert connector._initialized is True
    assert connector._retry_task is None
    assert KubectlConnector.isConnected is False


def test_a_kubectl_start_is_refused_and_recorded() -> None:
    """The guard the autouse fixture arms, measured on a list of its own."""
    with refusing_cluster_commands() as started:
        with pytest.raises(ClusterCommandInUnitLayerError):
            subprocess.run(["kubectl", "version"], check=False)
        assert started == ["kubectl"]


def test_another_command_passes_through_the_guard() -> None:
    with refusing_cluster_commands() as started:
        assert subprocess.run(["true"], check=False).returncode == 0
        assert started == []


def test_the_live_groups_and_the_integration_layer_keep_the_probe() -> None:
    assert measures_a_real_cluster(_CollectedTest(Path("tests/integration/test_kubernetes_live.py")))
    assert measures_a_real_cluster(_CollectedTest(Path("tests/e2e/test_sandbox_flows.py")))
    assert measures_a_real_cluster(_CollectedTest(Path("tests/test_x.py"), ["kubernetes_live"]))
    assert not measures_a_real_cluster(_CollectedTest(Path("tests/test_x.py")))


@pytest.mark.parametrize(
    "marker",
    [
        "requires_infra",
        "keycloak_live",
        "postgres_live",
        "minio_live",
        "forgejo_live",
        "kubernetes_live",
        "argocd_live",
        "startup_live",
        "tasks_live",
    ],
)
def test_every_live_group_from_the_docs_keeps_the_probe(marker: str) -> None:
    """Measured gap: four of these (keycloak_live, postgres_live, minio_live, forgejo_live)
    were missing, so a live test outside tests/integration/ with one of them would have its
    probe stripped and measure a mock instead of the real dependency."""
    assert measures_a_real_cluster(_CollectedTest(Path("tests/test_x.py"), [marker]))
