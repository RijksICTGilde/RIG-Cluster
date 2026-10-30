"""De platformkaart: de Operations Manager en de systemen eromheen, voor DIT cluster.

De data komt uit ``platform.json``, dat in de repo gegenereerd wordt
(:mod:`opi.tools.zad_map_platform`): ``infrastructure/`` en ``features/`` zitten niet in het
image, dus tijdens het draaien is er niets anders te lezen.

Er is geen schakelaar: de pagina toont het cluster dat deze OPI beheert.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from opi.core.config import settings
from opi.services.zad_map.contract import Color, Column, Edge, Graph, Group, Info, InfoItem, InfoSection, Node

PLATFORM_JSON = Path(__file__).resolve().parent / "platform.json"

#: Van het cluster dat OPI beheert naar de mapnaam in ``infrastructure/bootstrap/clusters``.
CLUSTER_FOLDER = {
    "odcn-production": "odcn",
    "odcn-staging": "odcn-staging",
    "sandboxed-local": "sandboxed-local",
    "local": "sandboxed-local",
}

#: De kaders rechts van de hub, en de band eronder.
LAYERS: dict[str, tuple[str, str]] = {
    "gitops": ("GitOps", "hoe projecten uitgerold worden"),
    "services": ("Services voor projecten", "wat ZAD per project inricht"),
    "network": ("Netwerk en DNS", "hoe projecten bereikbaar worden"),
}

#: De groep van een systeem bepaalt zijn kleur op de kaart.
GROUP_COLOR: dict[str, Color] = {
    "gitops": "gitops",
    "identity": "identity",
    "data": "data",
    "link": "link",
    "network": "network",
    "ops": "ops",
    "platform": "platform",
}


@cache
def _platform_data() -> dict[str, Any]:
    return json.loads(PLATFORM_JSON.read_text(encoding="utf-8"))


def current_cluster_folder() -> str:
    """De mapnaam van het cluster dat deze OPI beheert; de sandbox is de terugval."""
    return CLUSTER_FOLDER.get(settings.CLUSTER_MANAGER, "sandboxed-local")


def _visible(system: dict[str, Any], cluster: str) -> bool:
    """Wat er op deze kaart hoort: wat draait, wat elders of extern draait, wat een
    connector heeft, en op productie ook wat het cluster zelf levert."""
    state = system["clusters"].get(cluster, "afwezig")
    return (
        state in ("aan", "elders", "extern") or bool(system["connectors"]) or (state == "cluster" and cluster == "odcn")
    )


def _system_node(system: dict[str, Any], cluster: str, group: str) -> Node:
    state = system["clusters"].get(cluster, "afwezig")
    sections = [
        InfoSection(heading="Wat het is", items=[InfoItem(text=system["what"])]),
        InfoSection(heading="In dit cluster", items=[InfoItem(text=state)]),
    ]
    if system["connectors"]:
        sections.append(
            InfoSection(
                heading="Connector in OPI",
                items=[
                    InfoItem(text=f"{connector['file']} ({connector['uses']}x geimporteerd)", mono=True)
                    for connector in system["connectors"]
                ],
            )
        )
    if system["services"]:
        sections.append(
            InfoSection(
                heading="Services die hierop leunen",
                items=[InfoItem(text=service["title"]) for service in system["services"]],
            )
        )
    if system["docs"]:
        # Alleen tekst, geen links: features/ zit niet in het image.
        sections.append(
            InfoSection(
                heading="Meer lezen",
                items=[InfoItem(text=f"{doc['title']} ({doc['file']})") for doc in system["docs"]],
            )
        )
    return Node(
        id=f"sys:{system['id']}",
        group=group,
        shape="card",
        color=GROUP_COLOR.get(system["group"], "platform"),
        title=system["title"],
        subtitle=", ".join(service["title"] for service in system["services"][:2]),
        state=state if state in ("elders", "extern", "uit") else None,
        click="focus",
        info=Info(eyebrow=system["group"], title=system["title"], sections=sections),
    )


def platform_graph() -> Graph:
    """De plaat van het platform: ZAD links, de kaders rechts, het fundament eronder."""
    data = _platform_data()
    cluster = current_cluster_folder()
    shown = [system for system in data["systems"] if _visible(system, cluster)]
    in_layer = lambda layer: [system for system in shown if system["layer"] == layer]  # noqa: E731

    graph = Graph(columns=[Column(id="c0"), Column(id="c1")])
    graph.groups.append(Group(id="L:zad", title="ZAD", column="c0", pack="elk"))
    graph.nodes.append(
        Node(
            id="opi",
            group="L:zad",
            shape="hub",
            color="platform",
            eyebrow="ZAD",
            title="Operations Manager",
            click="info",
            info=Info(
                eyebrow="ZAD",
                title="Operations Manager",
                sections=[
                    InfoSection(
                        items=[
                            InfoItem(
                                text=(
                                    "De FastAPI-applicatie achter ZAD, een per cluster. Leest de projectbestanden, "
                                    "richt de services in via de connectors en schrijft de manifests naar Git, "
                                    "waarna ArgoCD uitrolt."
                                )
                            )
                        ]
                    )
                ],
            ),
        )
    )
    for system in in_layer("zad"):
        graph.nodes.append(_system_node(system, cluster, "L:zad"))

    for layer, (title, note) in LAYERS.items():
        group_id = f"L:{layer}"
        # GitOps laat ELK zijn inhoud neerleggen: daar loopt een lijn tussen twee kaarten.
        graph.groups.append(
            Group(id=group_id, title=title, subtitle=note, column="c1", pack="elk" if layer == "gitops" else "grid")
        )
        for system in in_layer(layer):
            graph.nodes.append(_system_node(system, cluster, group_id))
        graph.edges.append(Edge(from_="opi", to=group_id, color="platform"))

    ids = {node.id for node in graph.nodes}
    if {"sys:git", "sys:argocd"} <= ids:
        # ArgoCD leest wat ZAD in Git zet; in de sandbox staat Git op Forgejo.
        graph.edges.append(Edge(from_="sys:git", to="sys:argocd", color="gitops"))

    base = in_layer("odcn")
    if base:
        sandbox = cluster == "sandboxed-local"
        graph.groups.append(
            Group(
                id="L:odcn",
                title="Cluster-fundament" if sandbox else "Geleverd door ODCN",
                subtitle="Kind, met onze eigen aanvullingen" if sandbox else "het fundament onder alles",
                band=True,
                pack="row",
            )
        )
        for system in base:
            node = _system_node(system, cluster, "L:odcn")
            # In de band is van buiten komen de norm, geen afwijking.
            node.state = None
            graph.nodes.append(node)
    return graph
