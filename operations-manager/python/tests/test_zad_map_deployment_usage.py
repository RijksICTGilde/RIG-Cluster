"""The project map shows per deployment where its data sits, its backups and its sleep rule.

Lines go to the Deployments frame, not to each card: the frame packs its cards itself, so
a line cannot reach one of them. The card says what it holds instead.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
            }
        ],
        "deployments": [
            {
                "name": "productie",
                "cluster": "local",
                "namespace": "demo",
                "components": [{"reference": "web", "image": "nginx:1"}],
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


@pytest.fixture
def store() -> Any:
    summary = ProjectSummary(name=PROJECT, filename=f"{PROJECT}.yaml", data=_project())
    mock = MagicMock()
    mock.get.side_effect = {PROJECT: summary}.get
    mock.get_all.return_value = [summary]
    with (
        patch("opi.services.zad_map.project.get_project_store", return_value=mock),
        patch("opi.services.zad_map.project.get_prefixed_namespace", side_effect=lambda _c, ns: f"rig-{ns}"),
    ):
        yield mock


def _rows(graph: Graph, deployment: str) -> list[str]:
    node = next(node for node in graph.nodes if node.id == f"d:{deployment}")
    return [row.text for row in node.rows]


def test_the_card_shows_the_measured_sizes(store: Any) -> None:
    usage = MeasuredUsage(
        volume_used={("rig-demo", "productie-web-data-pvc"): 340e6},
        volume_capacity={("rig-demo", "productie-web-data-pvc"): 1e9},
        database_size={"demo_productie": 1.2e9},
    )

    graph = project_graph(PROJECT, usage)

    assert _rows(graph, "productie")[:2] == ["database: 1,2 GB", "web/data: 340 MB / 1 GB"]


def test_an_unmeasured_store_says_so_instead_of_zero(store: Any) -> None:
    graph = project_graph(PROJECT)

    assert _rows(graph, "pr-12")[:2] == ["database: niet gemeten", "web/data: niet gemeten"]


def test_data_services_have_a_line_to_the_deployments_frame(store: Any) -> None:
    graph = project_graph(PROJECT)

    assert any(edge.from_ == "s:postgresql-database" and edge.to == "deps" for edge in graph.edges)


def test_backups_are_a_service_for_the_deployments_that_have_a_schedule(store: Any) -> None:
    graph = project_graph(PROJECT)

    backups = next(node for node in graph.nodes if node.id == "backups")
    assert backups.info is not None
    assert [item.text for item in backups.info.sections[0].items] == ["productie: dagelijks rond 02:00"]
    assert any(edge.from_ == "backups" and edge.to == "deps" for edge in graph.edges)
    assert "backups: dagelijks rond 02:00" in _rows(graph, "productie")
    assert not any(text.startswith("backups") for text in _rows(graph, "pr-12"))


def test_no_backups_node_without_a_schedule(store: Any) -> None:
    data = _project()
    del data["deployments"][0]["backup"]
    store.get.side_effect = {PROJECT: ProjectSummary(name=PROJECT, filename="demo.yaml", data=data)}.get

    graph = project_graph(PROJECT)

    assert not any(node.id == "backups" for node in graph.nodes)


def test_the_sleep_rule_is_a_dashed_line_with_its_patterns(store: Any) -> None:
    graph = project_graph(PROJECT)

    (rule,) = [edge for edge in graph.edges if edge.from_ == "s:sleep-mode"]
    assert (rule.to, rule.style, rule.label) == ("deps", "dashed", "pr-*, *-preview")
    # The card says it, by the sweeper's own match.
    assert "slaapstand na 48h" in _rows(graph, "pr-12")
    assert not any(text.startswith("slaapstand") for text in _rows(graph, "productie"))


def test_no_sleep_line_without_patterns(store: Any) -> None:
    data = _project()
    data["services"][1]["config"]["match"] = []
    store.get.side_effect = {PROJECT: ProjectSummary(name=PROJECT, filename="demo.yaml", data=data)}.get

    graph = project_graph(PROJECT)

    assert not any(edge.from_ == "s:sleep-mode" for edge in graph.edges)


def test_the_targets_name_the_namespaces_and_databases(store: Any) -> None:
    assert usage_targets(PROJECT) == ({"rig-demo", "rig-demo-pr-12"}, {"demo_productie", "demo_pr_12"})


def test_the_output_validates_against_the_contract(store: Any) -> None:
    Graph.model_validate(project_graph(PROJECT).as_json())


@pytest.mark.asyncio
async def test_a_failing_metrics_store_costs_the_sizes_not_the_map() -> None:
    connector = MagicMock()
    connector.custom_query = AsyncMock(side_effect=PrometheusQueryError("down"))
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
