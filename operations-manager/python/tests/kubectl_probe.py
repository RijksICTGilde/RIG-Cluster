"""The kubectl connection probe, kept out of the test layers that have no cluster.

``KubectlConnector.__init__`` runs a blocking ``subprocess.run(["kubectl", "auth", "whoami"],
timeout=10)`` and schedules an async retry task when that fails. The singleton is reset
around every test, and several test files reset it again themselves, so every test that
builds a connector (also through ``ProjectManager``) starts that process again: ten seconds
each when the cluster is unreachable, and in an E2E run ten seconds in the middle of the
uvicorn event loop. Measured with seed 404: the stall landed on whichever test was unlucky in
the shuffle and took its neighbour down with it, which is what made that suite look order
dependent.

Only ``tests/integration/`` (the live groups) and ``tests/e2e/`` (the sandbox suite) have a
cluster. Everywhere else ``__init__`` is replaced for the duration of a test, and a start of
``kubectl`` that slips through fails the test instead of slowing it down.
"""

from __future__ import annotations

import asyncio
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from opi.utils.subprocess_env import child_env

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest

#: The test directories that bring their own cluster.
CLUSTER_DIRECTORIES = frozenset({"integration", "e2e"})

#: The live groups measure the real dependency (docs/development/toetsen.md). The markers
#: are here as well as the directories, so a live test placed elsewhere keeps its probe.
CLUSTER_MARKERS = frozenset(
    {
        "requires_infra",
        "keycloak_live",
        "postgres_live",
        "minio_live",
        "forgejo_live",
        "kubernetes_live",
        "argocd_live",
        "startup_live",
        "tasks_live",
    }
)

#: A test with this marker starts a kubectl of its own: a stub on PATH that records what the
#: process table and the pipe showed. It keeps the probe out but passes the guard.
OWN_KUBECTL_MARKER = "own_kubectl"

#: The command that only a layer with a cluster may start.
CLUSTER_COMMAND = "kubectl"

#: Captured before any test can patch them, so the guard wraps the real ones and the
#: own-kubectl escape can put them back.
_REAL_RUN = subprocess.run
_REAL_SUBPROCESS_EXEC = asyncio.create_subprocess_exec


class ClusterCommandInUnitLayerError(AssertionError):
    """A test outside the layers with a cluster started a cluster command."""


def measures_a_real_cluster(item: pytest.Item) -> bool:
    """Whether this test talks to a cluster of its own instead of a mocked one."""
    if CLUSTER_DIRECTORIES & set(item.path.parts):
        return True
    return bool(CLUSTER_MARKERS & {marker.name for marker in item.iter_markers()})


def _init_without_probing(connector: Any) -> None:
    """What ``KubectlConnector.__init__`` does, without the probe and its retry task."""
    if connector._initialized:
        return
    connector.env = child_env()
    connector._retry_task = None
    connector._initialized = True


def preinitialize_kubectl_without_probing() -> None:
    """Put an initialised KubectlConnector singleton in place, so nothing probes a cluster.

    For a process that outlives a single test, such as the E2E app server: there a patch on
    ``__init__`` would be gone by the time a request builds a connector.
    """
    from opi.connectors.kubectl import KubectlConnector

    connector = KubectlConnector.__new__(KubectlConnector)
    connector._initialized = False
    _init_without_probing(connector)
    KubectlConnector._instance = connector
    KubectlConnector.isConnected = False
    KubectlConnector._retry_task = None


@contextmanager
def kubectl_arrangement_for(item: pytest.Item) -> Iterator[None]:
    """Hold this test's arrangement around kubectl for its duration.

    A test with a cluster of its own keeps the real probe. Everywhere else ``__init__`` skips
    the probe, also when the test resets the singleton itself, and ``isConnected`` is False:
    that is what the probe concludes in a run without a cluster, so the layer runs through the
    same branches as CI.
    """
    from opi.connectors.kubectl import KubectlConnector

    if measures_a_real_cluster(item):
        yield
        return

    KubectlConnector.isConnected = False
    with patch.object(KubectlConnector, "__init__", _init_without_probing):
        if item.get_closest_marker(OWN_KUBECTL_MARKER):
            yield
            return
        with refusing_cluster_commands() as started:
            yield
    assert not started, f"this test started a cluster command while this layer has no cluster: {started}"


@contextmanager
def refusing_cluster_commands() -> Iterator[list[str]]:
    """Fail every start of ``kubectl``, and record it, for the duration of the block.

    Recording as well as raising, because ``KubectlConnector`` catches a broad ``Exception``
    around its probe: an exception on its own disappears there without failing anything, so
    the caller asserts on the recorded list.
    """
    started: list[str] = []

    def run(args: Any, *rest: Any, **kwargs: Any) -> Any:
        _refuse(args[0] if isinstance(args, (list, tuple)) and args else args, started)
        return _REAL_RUN(args, *rest, **kwargs)

    async def create_subprocess_exec(program: Any, *args: Any, **kwargs: Any) -> Any:
        _refuse(program, started)
        return await _REAL_SUBPROCESS_EXEC(program, *args, **kwargs)

    with patch("subprocess.run", run), patch("asyncio.create_subprocess_exec", create_subprocess_exec):
        yield started


def _refuse(command: Any, started: list[str]) -> None:
    if not isinstance(command, (str, Path)) or Path(command).name != CLUSTER_COMMAND:
        return
    started.append(str(command))
    raise ClusterCommandInUnitLayerError(
        f"this test starts {CLUSTER_COMMAND} and this layer has no cluster: mock the connector, "
        f"mark it {OWN_KUBECTL_MARKER} when it starts a stub of its own, or move it to tests/integration/"
    )
