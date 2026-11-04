"""The map of one deployment: what runs in it, how much data it keeps, its backups and sleep rule.

The project map shows the definition. Picking a deployment narrows it to the components
that run there, and puts the measured sizes on the services: with one deployment in view
it is unambiguous whose data it is.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.grafana_prometheus import GrafanaConnectionError
from opi.connectors.prometheus import PrometheusQueryError
from opi.services.project_service import ProjectSummary
from opi.services.zad_map.contract import Graph
from opi.services.zad_map.project import project_graph, usage_targets
from opi.services.zad_map.usage import MeasuredUsage, format_bytes, measure_usage

PROJECT = "demo"


def _project() -> dict[str, Any]:
    return {
        "name": PROJECT,
        "services": [
            "postgresql-database",
            {"name": "sleep-mode", "config": {"enabled": True, "match": ["pr-*", "*-preview"]}},
        ],
        "components": [
            {
                "name": "web",
                "type": "single",
                "services": [
                    "postgresql-database",
                    {"persistent-storage": {"config": [{"name": "data", "size": "1Gi", "mount-path": "/data"}]}},
                ],
            },
            {"name": "worker", "type": "single", "services": []},
        ],
        "deployments": [
            {
                "name": "productie",
                "cluster": "local",
                "namespace": "demo",
                "components": [{"reference": "web", "image": "nginx:1"}, {"reference": "worker", "image": "w:1"}],
                "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"},
            },
            {
                "name": "pr-12",
                "cluster": "local",
                "namespace": "demo-pr-12",
                "components": [{"reference": "web", "image": "nginx:2"}],
            },
        ],
    }


def _summary(data: dict[str, Any]) -> ProjectSummary:
    return ProjectSummary(name=PROJECT, filename=f"{PROJECT}.yaml", data=data)


@pytest.fixture
def store() -> Any:
    mock = MagicMock()
    mock.get.side_effect = {PROJECT: _summary(_project())}.get
    mock.get_all.return_value = []
    with (
        patch("opi.services.zad_map.project.get_project_store", return_value=mock),
        patch("opi.services.zad_map.project.get_prefixed_namespace", side_effect=lambda _cluster, ns: f"rig-{ns}"),
    ):
        yield mock


def _node(graph: Graph, node_id: str) -> Any:
    return next((node for node in graph.nodes if node.id == node_id), None)


def _rows(graph: Graph, node_id: str) -> list[str]:
    return [row.text for row in _node(graph, node_id).rows]


USAGE = MeasuredUsage(
    volume_used={("rig-demo", "productie-web-data-pvc"): 340e6},
    volume_capacity={("rig-demo", "productie-web-data-pvc"): 1e9},
    database_size={"demo_productie": 1.2e9},
)


@pytest.mark.usefixtures("store")
def test_only_the_components_that_run_in_the_deployment() -> None:
    graph = project_graph(PROJECT, "pr-12")

    assert [node.id for node in graph.nodes if node.id.startswith("c:")] == ["c:web"]
    assert _rows(graph, "c:web") == ["nginx:2"]


@pytest.mark.usefixtures("store")
def test_the_sizes_sit_on_the_services() -> None:
    graph = project_graph(PROJECT, "productie", USAGE)

    assert _rows(graph, "s:postgresql-database") == ["1,2 GB"]
    assert _rows(graph, "s:persistent-storage") == ["web/data: 340 MB / 1 GB"]


@pytest.mark.usefixtures("store")
def test_an_unmeasured_store_says_so_instead_of_zero() -> None:
    graph = project_graph(PROJECT, "pr-12")

    assert _rows(graph, "s:postgresql-database") == ["niet gemeten"]
    assert _rows(graph, "s:persistent-storage") == ["web/data: niet gemeten"]


@pytest.mark.usefixtures("store")
def test_backups_follow_the_services_they_cover() -> None:
    graph = project_graph(PROJECT, "productie")

    assert _rows(graph, "backups") == ["dagelijks rond 02:00"]
    assert {edge.from_ for edge in graph.edges if edge.to == "backups"} == {
        "s:postgresql-database",
        "s:persistent-storage",
    }


@pytest.mark.usefixtures("store")
def test_no_backups_node_without_a_schedule() -> None:
    graph = project_graph(PROJECT, "pr-12")

    assert _node(graph, "backups") is None


@pytest.mark.usefixtures("store")
def test_the_sleep_rule_says_whether_this_deployment_sleeps() -> None:
    assert _rows(project_graph(PROJECT, "pr-12"), "s:sleep-mode") == ["slaapt na 48h"]
    assert _rows(project_graph(PROJECT, "productie"), "s:sleep-mode") == ["slaapt niet vanzelf"]


@pytest.mark.usefixtures("store")
def test_the_project_map_measures_nothing_and_shows_the_definition() -> None:
    graph = project_graph(PROJECT)

    assert {node.id for node in graph.nodes if node.id.startswith("c:")} == {"c:web", "c:worker"}
    assert _rows(graph, "s:postgresql-database") == []
    assert _node(graph, "backups") is None


@pytest.mark.usefixtures("store")
def test_the_targets_are_those_of_the_one_deployment() -> None:
    assert usage_targets(PROJECT, "productie") == ({"rig-demo"}, {"demo_productie"})
    assert usage_targets(PROJECT, "bestaat-niet") == (set(), set())


@pytest.mark.usefixtures("store")
def test_the_output_validates_against_the_contract() -> None:
    Graph.model_validate(project_graph(PROJECT, "productie", USAGE).as_json())


@pytest.mark.asyncio
async def test_a_failing_metrics_store_costs_the_sizes_not_the_map() -> None:
    connector = MagicMock()
    connector.custom_query = AsyncMock(side_effect=PrometheusQueryError("down"))
    with patch("opi.services.zad_map.usage.get_metrics_connector", AsyncMock(return_value=connector)):
        usage = await measure_usage({"rig-demo"}, {"demo_productie"})

    assert usage == MeasuredUsage()


@pytest.mark.asyncio
async def test_an_unavailable_metrics_backend_costs_the_sizes_not_the_map() -> None:
    """The same outcome as a failing query: no sizes, and a map that still renders."""
    connector = MagicMock()
    connector.custom_query = AsyncMock(side_effect=GrafanaConnectionError("not available"))
    with patch("opi.services.zad_map.usage.get_metrics_connector", AsyncMock(return_value=connector)):
        usage = await measure_usage({"rig-demo"}, {"demo_productie"})

    assert usage == MeasuredUsage()


@pytest.mark.asyncio
async def test_measurements_are_keyed_by_namespace_pvc_and_database() -> None:
    def answer(query: str) -> list[dict[str, Any]]:
        if "used_bytes" in query:
            return [{"metric": {"namespace": "rig-demo", "persistentvolumeclaim": "p"}, "value": [0, "10"]}]
        if "capacity_bytes" in query:
            return [{"metric": {"namespace": "rig-demo", "persistentvolumeclaim": "p"}, "value": [0, "20"]}]
        return [{"metric": {"datname": "demo_productie"}, "value": [0, "30"]}]

    connector = MagicMock()
    connector.custom_query = AsyncMock(side_effect=answer)
    with patch("opi.services.zad_map.usage.get_metrics_connector", AsyncMock(return_value=connector)):
        usage = await measure_usage({"rig-demo", 'bad"name'}, {"demo_productie"})

    assert usage == MeasuredUsage(
        volume_used={("rig-demo", "p"): 10.0},
        volume_capacity={("rig-demo", "p"): 20.0},
        database_size={"demo_productie": 30.0},
    )
    # An unsafe name never reaches the query.
    assert all('bad"name' not in call.args[0] for call in connector.custom_query.await_args_list)


@pytest.mark.parametrize(
    ("value", "text"),
    [(512, "512 B"), (340e6, "340 MB"), (1e9, "1 GB"), (1.2e9, "1,2 GB"), (12.4e9, "12 GB")],
)
def test_format_bytes(value: float, text: str) -> None:
    assert format_bytes(value) == text
