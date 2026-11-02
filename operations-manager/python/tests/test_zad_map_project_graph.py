"""De projectkaart: wat er in de graaf staat, en wat er NIET in staat.

Vier dingen staan hier vast. Een koppeling naar een ander project staat er in BEIDE
richtingen in, met de naam van dat project en de poort als label. Een deployment klapt
open naar precies de componenten die erin zitten, met het webadres dat publish-on-web
afleidt. Een component zonder ``resources`` krijgt geen staafjes in plaats van de
platformstandaard. En de uitvoer valideert tegen het contract.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from opi.services.project_service import ProjectSummary
from opi.services.zad_map.contract import Graph
from opi.services.zad_map.project import incoming_links, project_graph

CLUSTER = "local"


def _demo() -> dict[str, Any]:
    return {
        "name": "demo",
        "display-name": "Demo",
        "description": "Een project om op te meten.",
        "users": [{"email": "beheerder@rijksoverheid.nl", "role": "admin"}],
        "services": [
            "publish-on-web",
            {
                "name": "cross-domain-access",
                "config": {
                    "outbound": [
                        {
                            "name": "naar-ander",
                            "from": {"component": "editor"},
                            "to": {"project": "ander", "deployment": "prod", "component": "redis", "port": 6379},
                        }
                    ]
                },
            },
        ],
        "components": [
            {
                "name": "editor",
                "type": "single",
                "services": ["publish-on-web", {"health-check": {"config": {"liveness-path": "/healthz"}}}],
                "resources": {"requests": {"memory": "128Mi", "cpu": "50m"}, "limits": {"memory": "512Mi", "cpu": "1"}},
            },
            {"name": "kaal", "type": "frontend", "services": []},
            # De oudere platte vorm; de handler leest hem niet en vult dan de
            # platformstandaard in, en die hoort niet als staafje op de kaart.
            {"name": "plat", "type": "frontend", "services": [], "resources": {"cpu": "250m", "memory": "256Mi"}},
            # Alleen een geheugenlimit: de cpu-rij zou anders de platformstandaard tonen.
            {"name": "half", "type": "frontend", "services": [], "resources": {"limits": {"memory": "256Mi"}}},
        ],
        "deployments": [
            {
                "name": "prod",
                "cluster": CLUSTER,
                "namespace": "demo",
                # Maar EEN van de twee componenten draait hier.
                "components": [{"reference": "editor", "image": "ghcr.io/x/editor:1.2.3"}],
            },
            {
                "name": "test",
                "cluster": CLUSTER,
                "namespace": "demo-test",
                "components": [{"reference": "editor", "image": "ghcr.io/x/editor:1.2.2"}],
                "services": [{"name": "cross-domain-access", "config": {"outbound": []}}],
            },
        ],
    }


def _other() -> dict[str, Any]:
    return {
        "name": "ander",
        "display-name": "Ander",
        "components": [{"name": "caller", "type": "single"}],
        "deployments": [{"name": "prod", "cluster": CLUSTER, "namespace": "ander", "components": []}],
        "services": [
            {
                "name": "cross-domain-access",
                "config": {
                    "outbound": [
                        {
                            "name": "naar-demo",
                            "from": {"component": "caller"},
                            "to": {"project": "demo", "deployment": "prod", "component": "editor", "port": 8000},
                        }
                    ]
                },
            }
        ],
    }


def _store() -> MagicMock:
    projects = {
        name: ProjectSummary(name=name, filename=f"{name}.yaml", data=data)
        for name, data in (("demo", _demo()), ("ander", _other()))
    }
    store = MagicMock()
    store.get.side_effect = projects.get
    store.get_all.return_value = list(projects.values())
    return store


@pytest.fixture
def graph() -> Graph:
    with patch("opi.services.zad_map.project.get_project_store", return_value=_store()):
        return project_graph("demo")


def _node(graph: Graph, node_id: str) -> Any:
    return next((node for node in graph.nodes if node.id == node_id), None)


def test_links_are_shown_in_both_directions(graph: Graph) -> None:
    outgoing = [edge for edge in graph.edges if edge.from_ == "c:editor" and edge.to.startswith("xc:")]
    incoming = [edge for edge in graph.edges if edge.to == "c:editor" and edge.from_.startswith("ic:")]
    assert [(edge.to, edge.label) for edge in outgoing] == [("xc:ander:redis", ":6379")]
    assert [(edge.from_, edge.label) for edge in incoming] == [("ic:ander:caller", ":8000")]
    # De projectnaam staat op het kader eromheen, een keer per richting.
    assert {group.id: group.title for group in graph.groups if group.style == "external"} == {
        "x:ander": "ander",
        "i:ander": "ander",
    }


def test_project_rule_does_not_yield_an_edge_per_deployment(graph: Graph) -> None:
    """De merge draait per deployment, dus een projectregel komt er net zo vaak uit.

    Zonder ontdubbeling krijgt een project met drie deployments drie lijnen naar hetzelfde
    doel, over elkaar heen getekend.
    """
    to_redis = [edge for edge in graph.edges if edge.to == "xc:ander:redis"]
    assert len(to_redis) == 1


def test_deployment_shows_only_its_own_components(graph: Graph) -> None:
    deployment = _node(graph, "d:prod")
    assert deployment is not None
    assert [child.title for child in deployment.children] == ["editor"]
    assert deployment.related == ["c:editor"]
    rows = deployment.children[0].rows
    assert rows[0].text == "editor:1.2.3"
    # Het webadres komt uit publish_on_web, niet uit een eigen afleiding.
    assert rows[1].href is not None
    assert rows[1].href.startswith("https://")


def test_component_without_resources_gets_no_bars(graph: Graph) -> None:
    bare = _node(graph, "c:kaal")
    assert bare is not None
    assert [row.bar for row in bare.rows] == []
    flat = _node(graph, "c:plat")
    assert flat is not None
    assert [row.bar for row in flat.rows] == []
    editor = _node(graph, "c:editor")
    assert editor is not None
    assert [row.bar for row in editor.rows if row.bar] == ["mem", "cpu"]


def test_bar_shows_only_what_the_component_declares(graph: Graph) -> None:
    half = _node(graph, "c:half")
    assert half is not None
    bars = [row for row in half.rows if row.bar]
    assert [(row.bar, row.req, row.lim, row.text) for row in bars] == [("mem", None, 256.0, "256Mi")]


def test_project_without_deployments_gets_no_empty_group() -> None:
    data = _demo()
    data["deployments"] = []
    store = MagicMock()
    store.get.side_effect = {"demo": ProjectSummary(name="demo", filename="demo.yaml", data=data)}.get
    store.get_all.return_value = []
    with patch("opi.services.zad_map.project.get_project_store", return_value=store):
        graph = project_graph("demo")
    assert [group.id for group in graph.groups if group.id == "deps"] == []


def test_output_validates_against_contract(graph: Graph) -> None:
    Graph.model_validate(graph.as_json())


def test_text_from_project_file_stays_text() -> None:
    """Alleen markdown UIT DE REPO komt als ``html`` binnen; een omschrijving nooit.

    Het element rendert ``html`` ongeescaped, dus een omschrijving met markup erin hoort
    in een ``text``-veld te belanden en nergens anders.
    """
    malicious = "<script>alert(1)</script>"
    data = _demo()
    data["description"] = malicious
    store = _store()
    store.get.side_effect = lambda name: (
        ProjectSummary(name="demo", filename="demo.yaml", data=data) if name == "demo" else None
    )
    with patch("opi.services.zad_map.project.get_project_store", return_value=store):
        graph = project_graph("demo")
    info = _node(graph, "project").info
    assert info is not None
    assert info.sections[0].items[0].text == malicious
    html = [section.html for node in graph.nodes if node.info for section in node.info.sections]
    assert not any(malicious in chunk for chunk in html)


def test_no_secrets_in_the_graph(graph: Graph) -> None:
    """De kaart leest de ruwe YAML; er mag niets van een geheim in belanden."""
    text = str(graph.as_json())
    assert "age-private-key" not in text
    assert "user-env-vars" not in text


def test_incoming_links_finds_the_sender() -> None:
    with patch("opi.services.zad_map.project.get_project_store", return_value=_store()):
        assert incoming_links("demo") == [("ander", "caller", "editor", 8000)]


def test_open_rule_becomes_the_all_projects_hub() -> None:
    """``from: {project: '*'}`` noemt geen zender; die knoop is ``incoming_links`` eigen,
    niet die van een ander project, en komt er als knoop "Alle projecten" op te staan."""
    data = _demo()
    cross_domain = next(
        entry for entry in data["services"] if isinstance(entry, dict) and entry.get("name") == "cross-domain-access"
    )
    cross_domain["config"]["inbound"] = [
        {"name": "open-naar-editor", "from": {"project": "*"}, "to": {"component": "editor", "port": 9000}}
    ]
    store = MagicMock()
    projects = {"demo": ProjectSummary(name="demo", filename="demo.yaml", data=data)}
    store.get.side_effect = projects.get
    store.get_all.return_value = list(projects.values())

    with patch("opi.services.zad_map.project.get_project_store", return_value=store):
        assert incoming_links("demo") == [("*", "", "editor", 9000)]
        graph = project_graph("demo")

    hub = _node(graph, "x:*")
    assert hub is not None
    assert hub.title == "Alle projecten"
    link_edge = next(edge for edge in graph.edges if edge.from_ == "x:*")
    assert (link_edge.to, link_edge.label) == ("c:editor", ":9000")


def test_unknown_project_raises_keyerror() -> None:
    with patch("opi.services.zad_map.project.get_project_store", return_value=_store()), pytest.raises(KeyError):
        project_graph("bestaat-niet")
