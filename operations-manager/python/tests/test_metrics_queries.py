"""The shared metrics queries: the fan-out over workloads and the targets it runs on."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from opi.services import metrics_queries
from opi.services.metrics_queries import (
    TimeWindow,
    deployment_pod_prefixes,
    discover_workloads,
    extract_workload_name_from_pod,
    namespace_selector,
    namespace_usage_queries,
    network_traffic_queries,
    pvc_storage,
    timeseries_per_workload,
    usage_per_namespace_queries,
    workload_pod_prefixes,
)


class FakeBackend:
    """A backend that records the queries it got and answers nothing by default."""

    is_connected = True

    def __init__(self) -> None:
        self.queries: list[dict[str, str]] = []

    async def query(self, query: str) -> list[dict[str, Any]]:
        return []

    async def query_batch(self, queries: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        self.queries.append(queries)
        return {}

    async def query_range_batch(
        self, queries: dict[str, str], start_time: Any, end_time: Any, step: str
    ) -> dict[str, list[dict[str, Any]]]:
        self.queries.append(queries)
        return {}


class TestTimeWindow:
    def test_step_is_the_promql_duration(self) -> None:
        assert TimeWindow(duration_minutes=120, step_minutes=15).step == "15m"

    def test_bounds_span_the_duration(self) -> None:
        start, end = TimeWindow(duration_minutes=90).bounds()
        assert round((end - start).total_seconds()) == 90 * 60


class TestPodPrefixes:
    def test_a_deployment_prefixes_its_components(self) -> None:
        assert deployment_pod_prefixes("production", ["api", "web"]) == {
            "api": "production-api",
            "web": "production-web",
        }

    def test_a_discovered_workload_is_its_own_prefix(self) -> None:
        assert workload_pod_prefixes([{"name": "frontend"}, {"name": "backend"}]) == {
            "frontend": "frontend",
            "backend": "backend",
        }

    def test_a_workload_without_a_name_is_skipped(self) -> None:
        assert workload_pod_prefixes([{"name": "frontend"}, {"name": ""}, {}]) == {"frontend": "frontend"}


class TestTimeseriesPerWorkload:
    @pytest.mark.asyncio
    async def test_workloads_are_fetched_concurrently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Three workloads start together; sequentially they would cost three times as long."""
        started: list[float] = []

        async def slow_component(backend: Any, namespace: str, pod_prefix: str, window: TimeWindow) -> dict[str, Any]:
            started.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.05)
            return {"pod_prefix": pod_prefix}

        monkeypatch.setattr(metrics_queries, "component_timeseries", slow_component)

        result = await timeseries_per_workload(
            FakeBackend(), "rig-demo", {"web": "p-web", "api": "p-api", "worker": "p-worker"}, TimeWindow()
        )

        assert set(result) == {"web", "api", "worker"}
        assert result["api"] == {"pod_prefix": "p-api"}
        assert len(started) == 3
        assert max(started) - min(started) < 0.02

    @pytest.mark.asyncio
    async def test_no_workloads_means_no_queries(self) -> None:
        backend = FakeBackend()

        assert await timeseries_per_workload(backend, "rig-demo", {}, TimeWindow()) == {}
        assert backend.queries == []


class VolumeBackend(FakeBackend):
    """A backend that answers the two volume queries from fixed series."""

    def __init__(self, used: list[dict[str, Any]], capacity: list[dict[str, Any]]) -> None:
        super().__init__()
        self._used = used
        self._capacity = capacity

    async def query_batch(self, queries: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        self.queries.append(queries)
        return {"capacity": self._capacity}

    async def query_range_batch(
        self, queries: dict[str, str], start_time: Any, end_time: Any, step: str
    ) -> dict[str, list[dict[str, Any]]]:
        self.queries.append(queries)
        return {"used": self._used}


class TestPvcStorage:
    """Used space per volume claim, with the capacity and the thresholds it implies."""

    @pytest.mark.asyncio
    async def test_a_claim_gets_its_capacity_and_thresholds(self) -> None:
        gb = 1024 * 1024 * 1024
        backend = VolumeBackend(
            used=[
                {
                    "metric": {"persistentvolumeclaim": "production-api-data"},
                    "values": [[1000, str(2 * gb)], [2000, str(3 * gb)]],
                }
            ],
            capacity=[{"metric": {"persistentvolumeclaim": "production-api-data"}, "value": [1000, str(10 * gb)]}],
        )

        result = await pvc_storage(backend, "rig-demo", TimeWindow())

        assert result["production-api-data"] == {
            "values": [{"timestamp": 1000, "value": 2.0}, {"timestamp": 2000, "value": 3.0}],
            "timestamps": [1000, 2000],
            "capacity_gb": 10.0,
            "warning_threshold_gb": 8.0,
            "critical_threshold_gb": 9.0,
        }

    @pytest.mark.asyncio
    async def test_a_claim_without_a_capacity_reading_reports_zero(self) -> None:
        backend = VolumeBackend(
            used=[{"metric": {"persistentvolumeclaim": "data"}, "values": [[1000, "0"]]}], capacity=[]
        )

        result = await pvc_storage(backend, "rig-demo", TimeWindow())

        assert result["data"]["capacity_gb"] == 0.0
        assert result["data"]["critical_threshold_gb"] == 0.0

    @pytest.mark.asyncio
    async def test_an_unavailable_backend_measures_nothing(self) -> None:
        backend = VolumeBackend(used=[], capacity=[])
        backend.is_connected = False

        assert await pvc_storage(backend, "rig-demo", TimeWindow()) == {}
        assert backend.queries == []


class PodBackend(FakeBackend):
    """A backend that answers ``kube_pod_info`` with fixed pods."""

    def __init__(self, pods: list[dict[str, str]]) -> None:
        super().__init__()
        self._pods = pods

    async def query(self, query: str) -> list[dict[str, Any]]:
        self.queries.append({"instant": query})
        return [{"metric": pod} for pod in self._pods]


class TestExtractWorkloadNameFromPod:
    @pytest.mark.parametrize(
        ("pod_name", "expected"),
        [
            ("frontend-7d9f8b6c4d-x2k9p", "frontend"),
            ("postgres-0", "postgres"),
            ("postgres-12", "postgres"),
            ("backup-29384-abcde", "backup"),
            ("single", "single"),
            ("web-abcde", "web"),
            # A name whose last part is five alphanumeric characters reads as a hash, so a
            # workload really called "two-parts" shows up as "two".
            ("two-parts", "two"),
        ],
    )
    def test_names(self, pod_name: str, expected: str) -> None:
        assert extract_workload_name_from_pod(pod_name) == expected


class TestDiscoverWorkloads:
    @pytest.mark.asyncio
    async def test_pods_are_grouped_by_owner(self) -> None:
        backend = PodBackend(
            [
                {"pod": "frontend-7d9f8b6c4d-x2k9p", "created_by_kind": "ReplicaSet"},
                {"pod": "frontend-7d9f8b6c4d-q4w8t", "created_by_kind": "ReplicaSet"},
                {"pod": "postgres-0", "created_by_kind": "StatefulSet"},
                {"pod": "node-exporter-abcde", "created_by_kind": "DaemonSet"},
            ]
        )

        result = await discover_workloads(backend, "rig-demo")

        assert [workload["name"] for workload in result] == ["frontend", "node", "postgres"]
        assert result[0] == {
            "name": "frontend",
            "pod_count": 2,
            "pods": ["frontend-7d9f8b6c4d-x2k9p", "frontend-7d9f8b6c4d-q4w8t"],
            "workload_type": "Deployment",
        }
        assert result[2]["workload_type"] == "StatefulSet"

    @pytest.mark.asyncio
    async def test_jobs_and_unowned_job_pods_are_left_out(self) -> None:
        backend = PodBackend(
            [
                {"pod": "migrate-abcde", "created_by_kind": "Job"},
                {"pod": "orphan-xyz12", "created_by_kind": ""},
                {"pod": "postgres-0", "created_by_kind": ""},
            ]
        )

        result = await discover_workloads(backend, "rig-demo")

        assert [workload["name"] for workload in result] == ["postgres"]
        assert result[0]["workload_type"] == "Unknown"

    @pytest.mark.asyncio
    async def test_an_unavailable_backend_discovers_nothing(self) -> None:
        backend = PodBackend([{"pod": "frontend-7d9f8b6c4d-x2k9p", "created_by_kind": "ReplicaSet"}])
        backend.is_connected = False

        assert await discover_workloads(backend, "rig-demo") == []
        assert backend.queries == []


class TestNamespaceQueries:
    """The usage queries the dashboard and the project page share."""

    def test_the_selector_keeps_the_order_it_was_given(self) -> None:
        assert namespace_selector(["rig-b", "rig-a"]) == 'namespace=~"rig-b|rig-a"'

    def test_totals_aggregate_over_the_namespaces(self) -> None:
        queries = namespace_usage_queries(["rig-demo", "rig-demo-infrastructure"])
        selector = 'namespace=~"rig-demo|rig-demo-infrastructure"'

        phase = f'max by (namespace, pod) (kube_pod_status_phase{{{selector},phase=~"Running|Pending"}} == 1)'

        assert queries["cpu_cores"] == f'sum(rate(container_cpu_usage_seconds_total{{{selector},container!=""}}[5m]))'
        assert queries["memory_limit_bytes"] == (
            f'sum(kube_pod_container_resource_limits{{{selector},resource="memory"}}'
            f" * on (namespace, pod) group_left () {phase})"
        )
        assert queries["storage_used_bytes"] == f"sum(kubelet_volume_stats_used_bytes{{{selector}}})"
        assert queries["storage_capacity_bytes"] == (
            f"sum(kube_persistentvolumeclaim_resource_requests_storage_bytes{{{selector}}})"
        )
        assert queries["pod_count"] == f"count(kube_pod_info{{{selector}}})"

    def test_the_per_namespace_variant_only_adds_the_grouping(self) -> None:
        """One query covers every project, so the cost does not grow with the project count."""
        totals = namespace_usage_queries(["rig-demo"])
        per_namespace = usage_per_namespace_queries(["rig-demo"])

        for key, query in per_namespace.items():
            assert query == totals[key].replace("sum(", "sum by (namespace) (", 1)

    def test_network_traffic_is_in_bytes_per_second(self) -> None:
        queries = network_traffic_queries(["rig-demo"])
        selector = 'namespace=~"rig-demo"'

        assert queries["network_in"] == f"sum(rate(container_network_receive_bytes_total{{{selector}}}[5m]))"
        assert queries["network_out"] == f"sum(rate(container_network_transmit_bytes_total{{{selector}}}[5m]))"
