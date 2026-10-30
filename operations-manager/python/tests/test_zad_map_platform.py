"""De platformkaart: het ingecheckte bestand volgt de repo, en de plaat volgt het cluster.

``platform.json`` wordt in de repo gegenereerd omdat ``infrastructure/`` en ``features/``
niet in het image zitten. Een ingecheckt bestand dat achterloopt op de repo is erger dan
geen bestand: het beweert iets over een cluster dat er niet meer zo uitziet.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from opi.core.config import settings
from opi.services.zad_map.contract import Graph
from opi.services.zad_map.platform import PLATFORM_JSON, current_cluster_folder, platform_graph
from opi.tools.zad_map_platform import ROOT, as_text, build


def test_checked_in_file_matches_the_repo() -> None:
    assert PLATFORM_JSON.read_text() == as_text(build(ROOT)), (
        "platform.json loopt achter op de repo. Bijwerken: uv run python -m opi.tools.zad_map_platform"
    )


@pytest.mark.parametrize(
    ("cluster_manager", "folder"),
    [
        ("odcn-production", "odcn"),
        ("odcn-staging", "odcn-staging"),
        ("sandboxed-local", "sandboxed-local"),
        ("local", "sandboxed-local"),
        ("iets-onbekends", "sandboxed-local"),
    ],
)
def test_current_cluster_folder_maps_cluster_manager(
    monkeypatch: pytest.MonkeyPatch, cluster_manager: str, folder: str
) -> None:
    monkeypatch.setattr(settings, "CLUSTER_MANAGER", cluster_manager)
    assert current_cluster_folder() == folder


def _graph(cluster: str) -> Graph:
    with patch("opi.services.zad_map.platform.current_cluster_folder", return_value=cluster):
        return platform_graph()


@pytest.mark.parametrize(
    ("cluster", "visible", "invisible"),
    [
        # Vault en pgAdmin staan in de odcn-kustomization uit, dus die hoef je daar niet
        # te zien; Forgejo bestaat alleen in de sandbox.
        ("odcn", "sys:keycloak", "sys:vault"),
        ("odcn", "sys:keycloak", "sys:pgadmin"),
        ("odcn", "sys:keycloak", "sys:forgejo"),
        ("sandboxed-local", "sys:forgejo", "sys:vault"),
    ],
)
def test_cluster_choice_decides_what_is_shown(cluster: str, visible: str, invisible: str) -> None:
    ids = {node.id for node in _graph(cluster).nodes}
    assert visible in ids
    assert invisible not in ids


def test_hub_and_groups_are_present() -> None:
    graph = _graph("odcn")
    assert any(node.id == "opi" and node.shape == "hub" for node in graph.nodes)
    groups = {group.id for group in graph.groups}
    assert {"L:zad", "L:gitops", "L:services", "L:network", "L:odcn"} <= groups
    # Een lijn vanaf de hub naar elk kader rechts, zonder label.
    from_hub = {edge.to: edge.label for edge in graph.edges if edge.from_ == "opi"}
    assert from_hub == {"L:gitops": "", "L:services": "", "L:network": ""}


def test_band_has_another_title_on_sandbox() -> None:
    band = lambda graph: next(group for group in graph.groups if group.band)  # noqa: E731
    assert band(_graph("odcn")).title == "Geleverd door ODCN"
    assert band(_graph("sandboxed-local")).title == "Cluster-fundament"
    # In de band is van buiten komen de norm, dus geen stippelrand.
    sandbox = _graph("sandboxed-local")
    assert all(node.state is None for node in sandbox.nodes if node.group == "L:odcn")


def test_output_validates_against_contract() -> None:
    Graph.model_validate(_graph("odcn").as_json())
