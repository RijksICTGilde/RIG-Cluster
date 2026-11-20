"""RC-513: OPI reads its routing stand from the cluster, and switches over exactly once.

The ConfigMap ``zad-routing`` is the source; ``CLUSTER_CONFIG`` is the fallback for a cluster
without it, so a read that fails may never read as "no ConfigMap". The switch waits until the
entrypoint of the new stand accepts routes, and queues one reprocess of every project. The
adopted stand is stored, so a restart neither repeats that reprocess nor skips it.

See docs/sandbox/routing-wisselen.md and features/gateway-api-routing.md.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.core import cluster_config
from opi.core.cluster_config import RoutingApi, get_routing_api
from opi.runtime.periodic import routing_mode_watcher
from opi.runtime.periodic.routing_mode_watcher import (
    RoutingModeUnknownError,
    RoutingModeWatcher,
    read_wanted_routing_api,
)
from opi.services.routing_readiness import RoutingEntrypointMissingError, RoutingEntrypointUnknownError

CLUSTER = "sandboxed-local"


class FakeKubectl:
    """``resource_exists`` separates absent from unreadable, as the connector does."""

    def __init__(self, *, exists: bool | None, data: dict[str, str] | None = None, readable: bool = True) -> None:
        self._exists = exists
        self._data = data
        self._readable = readable

    async def resource_exists(self, resource_type: str, name: str, namespace: str) -> bool | None:
        return self._exists

    async def get_namespaced_resource(self, resource_type: str, name: str, namespace: str) -> dict[str, Any] | None:
        if not self._readable:
            return None
        return {"data": self._data} if self._data is not None else {}


class FakeStore:
    def __init__(self, adopted: RoutingApi | None = None) -> None:
        self._adopted = adopted
        self.recorded: list[RoutingApi] = []

    async def adopted(self, cluster: str) -> RoutingApi | None:
        return self._adopted

    async def record(self, cluster: str, routing: RoutingApi) -> None:
        self._adopted = routing
        self.recorded.append(routing)


class FakeTaskService:
    def __init__(self) -> None:
        self.created: list[str] = []

    async def create_task(self, *, project_name: str, **_: Any) -> dict[str, str]:
        self.created.append(project_name)
        return {"task_id": f"task-{project_name}"}


class FakeProject:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeProjectStore:
    def __init__(self, cache_head: str | None = "abc1234") -> None:
        self._cache_head = cache_head

    def get_all(self) -> list[FakeProject]:
        """Empty without a loaded cache, as the real store is before its first load."""
        if self._cache_head is None:
            return []
        return [FakeProject("een"), FakeProject("twee")]

    def cache_head(self) -> str | None:
        return self._cache_head


@pytest.fixture(autouse=True)
def _no_stand_leaks_between_tests() -> Any:
    """``_rendered_routing_api`` is module state, so a switch in one test must not leak."""
    cluster_config._rendered_routing_api.clear()
    yield
    cluster_config._rendered_routing_api.clear()


@pytest.fixture
def projects(monkeypatch: pytest.MonkeyPatch) -> FakeProjectStore:
    return _project_store(monkeypatch, FakeProjectStore())


def _project_store(monkeypatch: pytest.MonkeyPatch, store: FakeProjectStore) -> FakeProjectStore:
    monkeypatch.setattr("opi.services.project_reprocess.get_project_store", lambda: store)
    monkeypatch.setattr(routing_mode_watcher, "get_project_store", lambda: store)
    return store


def _watch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kubectl: FakeKubectl,
    store: FakeStore,
    entrypoint: str | Exception = "ingang is klaar",
) -> tuple[RoutingModeWatcher, FakeTaskService]:
    monkeypatch.setattr(routing_mode_watcher, "create_kubectl_connector", lambda: kubectl)

    async def verify(cluster: str, routing: RoutingApi | None = None) -> str:
        if isinstance(entrypoint, Exception):
            raise entrypoint
        return entrypoint

    monkeypatch.setattr(routing_mode_watcher, "verify_routing_entrypoint", verify)
    tasks = FakeTaskService()
    return RoutingModeWatcher(tasks, CLUSTER, store=store), tasks  # type: ignore[arg-type]


async def test_the_configmap_is_the_source_of_the_stand(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        routing_mode_watcher, "create_kubectl_connector", lambda: FakeKubectl(exists=True, data={"routing": "gateway"})
    )

    assert await read_wanted_routing_api("rig-system") is RoutingApi.GATEWAY


async def test_no_configmap_leaves_cluster_config_in_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cluster without the ConfigMap keeps the stand of its configuration, so None."""
    monkeypatch.setattr(routing_mode_watcher, "create_kubectl_connector", lambda: FakeKubectl(exists=False))

    assert await read_wanted_routing_api("rig-system") is None


@pytest.mark.parametrize(
    "kubectl",
    [
        FakeKubectl(exists=None),
        FakeKubectl(exists=True, readable=False),
        FakeKubectl(exists=True, data={"routing": "iets-anders"}),
        FakeKubectl(exists=True, data={}),
    ],
    ids=["existence-unknown", "unreadable", "unknown-value", "key-missing"],
)
async def test_a_failed_read_is_never_no_configmap(monkeypatch: pytest.MonkeyPatch, kubectl: FakeKubectl) -> None:
    """Never None: that would read as "no ConfigMap" and fall back to the configuration."""
    monkeypatch.setattr(routing_mode_watcher, "create_kubectl_connector", lambda: kubectl)

    with pytest.raises(RoutingModeUnknownError):
        await read_wanted_routing_api("rig-system")


async def test_the_same_stand_does_not_switch(monkeypatch: pytest.MonkeyPatch, projects: FakeProjectStore) -> None:
    store = FakeStore()
    watcher, tasks = _watch(monkeypatch, kubectl=FakeKubectl(exists=True, data={"routing": "ingress"}), store=store)

    await watcher.tick()

    assert get_routing_api(CLUSTER) is RoutingApi.INGRESS
    assert tasks.created == []
    assert store.recorded == []


async def test_another_stand_waits_for_the_entrypoint(
    monkeypatch: pytest.MonkeyPatch, projects: FakeProjectStore
) -> None:
    store = FakeStore()
    watcher, tasks = _watch(
        monkeypatch,
        kubectl=FakeKubectl(exists=True, data={"routing": "gateway"}),
        store=store,
        entrypoint=RoutingEntrypointMissingError("de Gateway is niet Programmed"),
    )

    await watcher.tick()

    assert get_routing_api(CLUSTER) is RoutingApi.INGRESS, "until the entrypoint is ready OPI stays on the old stand"
    assert tasks.created == []
    assert store.recorded == []


async def test_an_unmeasurable_entrypoint_does_not_switch(
    monkeypatch: pytest.MonkeyPatch, projects: FakeProjectStore
) -> None:
    store = FakeStore()
    watcher, tasks = _watch(
        monkeypatch,
        kubectl=FakeKubectl(exists=True, data={"routing": "gateway"}),
        store=store,
        entrypoint=RoutingEntrypointUnknownError("kon de Gateway niet lezen"),
    )

    await watcher.tick()

    assert get_routing_api(CLUSTER) is RoutingApi.INGRESS
    assert store.recorded == []


async def test_with_the_entrypoint_ready_opi_switches_and_reprocesses_once(
    monkeypatch: pytest.MonkeyPatch, projects: FakeProjectStore
) -> None:
    store = FakeStore()
    watcher, tasks = _watch(monkeypatch, kubectl=FakeKubectl(exists=True, data={"routing": "gateway"}), store=store)

    await watcher.tick()

    assert get_routing_api(CLUSTER) is RoutingApi.GATEWAY
    assert tasks.created == ["een", "twee"]
    assert store.recorded == [RoutingApi.GATEWAY]

    await watcher.tick()

    assert tasks.created == ["een", "twee"], "a second tick does not reprocess again"


async def test_a_restart_after_a_switch_does_not_reprocess_again(
    monkeypatch: pytest.MonkeyPatch, projects: FakeProjectStore
) -> None:
    """A fresh process: the rendered stand is empty, but the switch is in the store."""
    store = FakeStore(adopted=RoutingApi.GATEWAY)
    watcher, tasks = _watch(monkeypatch, kubectl=FakeKubectl(exists=True, data={"routing": "gateway"}), store=store)

    await watcher.tick()

    assert get_routing_api(CLUSTER) is RoutingApi.GATEWAY, "OPI renders in the adopted stand again"
    assert tasks.created == []
    assert store.recorded == []


async def test_a_restart_halfway_a_switch_still_reprocesses(
    monkeypatch: pytest.MonkeyPatch, projects: FakeProjectStore
) -> None:
    """The ConfigMap already asks for gateway, the store still says ingress: the switch was
    never performed, so it must still happen."""
    store = FakeStore(adopted=RoutingApi.INGRESS)
    watcher, tasks = _watch(monkeypatch, kubectl=FakeKubectl(exists=True, data={"routing": "gateway"}), store=store)

    await watcher.tick()

    assert get_routing_api(CLUSTER) is RoutingApi.GATEWAY
    assert tasks.created == ["een", "twee"]
    assert store.recorded == [RoutingApi.GATEWAY]


async def test_a_failed_read_leaves_the_current_stand_in_place(
    monkeypatch: pytest.MonkeyPatch, projects: FakeProjectStore
) -> None:
    store = FakeStore()
    watcher, tasks = _watch(monkeypatch, kubectl=FakeKubectl(exists=None), store=store)

    await watcher.tick()

    assert get_routing_api(CLUSTER) is RoutingApi.INGRESS
    assert tasks.created == []


async def test_a_switch_back_to_the_configured_stand_still_reprocesses(
    monkeypatch: pytest.MonkeyPatch, projects: FakeProjectStore
) -> None:
    """The switch back finished while OPI was down: the ConfigMap and CLUSTER_CONFIG agree
    again, but the store still holds the stand of before, so the reprocess is still owed."""
    store = FakeStore(adopted=RoutingApi.GATEWAY)
    watcher, tasks = _watch(monkeypatch, kubectl=FakeKubectl(exists=True, data={"routing": "ingress"}), store=store)

    await watcher.tick()

    assert get_routing_api(CLUSTER) is RoutingApi.INGRESS
    assert tasks.created == ["een", "twee"]
    assert store.recorded == [RoutingApi.INGRESS]


async def test_after_a_switch_back_the_next_switch_is_not_treated_as_adopted(
    monkeypatch: pytest.MonkeyPatch, projects: FakeProjectStore
) -> None:
    """The store follows every switch, so a second switch to a stand adopted earlier waits
    for its entrypoint and reprocesses again instead of taking the adopted branch."""
    store = FakeStore(adopted=RoutingApi.GATEWAY)
    back, _ = _watch(monkeypatch, kubectl=FakeKubectl(exists=True, data={"routing": "ingress"}), store=store)
    await back.tick()

    forward, tasks = _watch(
        monkeypatch,
        kubectl=FakeKubectl(exists=True, data={"routing": "gateway"}),
        store=store,
        entrypoint=RoutingEntrypointMissingError("de Gateway is niet Programmed"),
    )
    await forward.tick()

    assert get_routing_api(CLUSTER) is RoutingApi.INGRESS, "the entrypoint is not ready, so no switch"
    assert tasks.created == []
    assert store.recorded == [RoutingApi.INGRESS]


async def test_an_unloaded_project_cache_does_not_mark_the_switch_as_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The project cache comes from a non-fatal startup step. Without it a reprocess would
    queue nothing, so the switch stays owed and the next tick tries again."""
    _project_store(monkeypatch, FakeProjectStore(cache_head=None))
    store = FakeStore()
    watcher, tasks = _watch(monkeypatch, kubectl=FakeKubectl(exists=True, data={"routing": "gateway"}), store=store)

    await watcher.tick()

    assert get_routing_api(CLUSTER) is RoutingApi.INGRESS
    assert tasks.created == []
    assert store.recorded == []
