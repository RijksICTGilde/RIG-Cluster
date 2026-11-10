"""The platform map: the Operations Manager and the systems around it, for THIS cluster.

The data comes from ``platform.json``, generated in the repo
(:mod:`opi.tools.zad_map_platform`). ``infrastructure/`` and ``features/`` are not in the
image, so at run time there is nothing else to read.

There is no switch: the page shows the cluster this OPI manages.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from opi.core.config import settings
from opi.services.zad_map.contract import Color, Column, Edge, Graph, Group, Info, InfoItem, InfoSection, Node
from opi.web.security_policy import REPO_BLOB_URL

PLATFORM_JSON = Path(__file__).resolve().parent / "platform.json"

#: From the cluster OPI manages to its folder name in ``infrastructure/bootstrap/clusters``.
CLUSTER_FOLDER = {
    "odcn-production": "odcn",
    "odcn-staging": "odcn-staging",
    "sandboxed-local": "sandboxed-local",
    "local": "sandboxed-local",
}

#: The frames to the right of the hub, and the band underneath.
LAYERS: dict[str, tuple[str, str]] = {
    "gitops": ("GitOps", "hoe projecten uitgerold worden"),
    "services": ("Services voor projecten", "wat ZAD per project inricht"),
    "network": ("Netwerk en DNS", "hoe projecten bereikbaar worden"),
}

#: A system's group decides its color on the map.
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
    """The folder name of the cluster this OPI manages; the sandbox is the fallback."""
    return CLUSTER_FOLDER.get(settings.CLUSTER_MANAGER, "sandboxed-local")


def _visible(system: dict[str, Any], cluster: str) -> bool:
    """What belongs on this map: what runs, what runs elsewhere or externally, what has a
    connector, and on production also what the cluster itself provides."""
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
        # features/ is not in the image, so the link goes to the file on GitHub main; the
        # panel opens an href in a new tab.
        sections.append(
            InfoSection(
                heading="Meer lezen",
                items=[InfoItem(text=doc["title"], href=REPO_BLOB_URL + doc["file"]) for doc in system["docs"]],
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
    """The platform drawing: ZAD on the left, the frames on the right, the base underneath."""
    data = _platform_data()
    cluster = current_cluster_folder()
    shown = [system for system in data["systems"] if _visible(system, cluster)]
    in_layer = lambda layer: [system for system in shown if system["layer"] == layer]  # noqa: E731  # a one-line filter over a local, used four times just below

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
        # GitOps leaves its contents to ELK, because an edge runs between two of its cards.
        graph.groups.append(
            Group(id=group_id, title=title, subtitle=note, column="c1", pack="elk" if layer == "gitops" else "grid")
        )
        for system in in_layer(layer):
            graph.nodes.append(_system_node(system, cluster, group_id))
        graph.edges.append(Edge(from_="opi", to=group_id, color="platform"))

    ids = {node.id for node in graph.nodes}
    if {"sys:git", "sys:argocd"} <= ids:
        # ArgoCD reads what ZAD puts in Git; on the sandbox Git is Forgejo.
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
            # In the band, coming from outside is the norm and not a deviation.
            node.state = None
            graph.nodes.append(node)
    return graph
