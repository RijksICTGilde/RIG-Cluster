"""De projectkaart: componenten, services, deployments en koppelingen als graaf.

Alles komt uit ``ProjectSummary.data``, de ruwe YAML. NOOIT uit ``get_decrypted``: op deze
kaart staat geen enkel geheim, en een env-var of wachtwoord hoort er ook niet per ongeluk
in te kunnen belanden.

Van een ANDER project wordt alleen gelezen wat in een cross-domain-regel staat die naar dit
project wijst: projectnaam, component en poort.
"""

from __future__ import annotations

import logging
import math
import re
from typing import TYPE_CHECKING, Any, Literal

from opi.handlers.project_file_handler import (
    ProjectFileHandler,
    _parse_resources_block_partial,
    extract_storage_from_component_services,
)
from opi.services.catalog.cross_domain_access.config_model import WILDCARD_PROJECT
from opi.services.catalog.cross_domain_access.merge import (
    IncompleteRuleError,
    MergedRule,
    direction_rules,
    merge_rules,
    to_merged_rule,
)
from opi.services.catalog.publish_on_web.urls import public_urls_for_deployment
from opi.services.help_text import render_service_help
from opi.services.project_store import get_project_store
from opi.services.registry import SERVICE_DEFINITIONS
from opi.services.services import ServiceAdapter, service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType
from opi.services.zad_map.contract import (
    Color,
    Column,
    Edge,
    Glyph,
    Graph,
    Group,
    Info,
    InfoItem,
    InfoSection,
    Node,
    Row,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Kleur per dienst op de kaart: presentatiekeuze, geen eigenschap van de dienst.
#: Diensten die hier niet staan krijgen ``ops``.
SERVICE_COLOR: dict[ServiceType, Color] = {
    ServiceType.PUBLISH_ON_WEB: "web",
    ServiceType.ALIASES: "web",
    ServiceType.KEYCLOAK: "identity",
    ServiceType.AUTHORIZATION_WALL: "identity",
    ServiceType.INVITE: "identity",
    ServiceType.POSTGRESQL_DATABASE: "data",
    ServiceType.NAMESPACE_POSTGRESQL_DATABASE: "data",
    ServiceType.REDIS: "data",
    ServiceType.NAMESPACE_REDIS: "data",
    ServiceType.MINIO_STORAGE: "data",
    ServiceType.SEAWEEDFS_STORAGE: "data",
    ServiceType.PERSISTENT_STORAGE: "data",
    ServiceType.TEMP_STORAGE: "data",
    ServiceType.ATTACHMENTS: "data",
    ServiceType.CROSS_DOMAIN_ACCESS: "link",
    ServiceType.VLAM: "link",
    ServiceType.SEND_EMAIL: "link",
}

#: Het aantal componenten dat onder elkaar past; daarboven komen er kolommen bij.
COMPONENTS_PER_COLUMN = 6

_MEMORY_UNITS = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "K": 1 / 1000, "M": 1, "G": 1000}
_MEMORY = re.compile(r"^([\d.]+)\s*(Ki|Mi|Gi|K|M|G)?$")
_CPU = re.compile(r"^([\d.]+)(m)?$")


def _memory_mi(value: Any) -> float | None:
    """Geheugen in Mi; None als er niets of iets onleesbaars staat."""
    match = _MEMORY.match(str(value or ""))
    return float(match.group(1)) * _MEMORY_UNITS.get(match.group(2) or "", 1 / (1024 * 1024)) if match else None


def _cpu_millicores(value: Any) -> float | None:
    match = _CPU.match(str(value or ""))
    return float(match.group(1)) * (1 if match.group(2) else 1000) if match else None


def _service_color(name: str) -> Color:
    try:
        return SERVICE_COLOR.get(ServiceType(name), "ops")
    except ValueError:
        return "ops"


def _service_title(name: str) -> str:
    try:
        return SERVICE_DEFINITIONS[ServiceType(name)].name
    except ValueError, KeyError:
        return name


def _component_service_config(component: dict[str, Any], service: ServiceType) -> Any:
    """De config van EEN dienst op een component, in welk schrijfwijze dan ook."""
    for entry in component.get("services") or []:
        if service_entry_name(entry) == service.value:
            return service_entry_config(entry)
    return None


def _component_services(component: dict[str, Any]) -> list[str]:
    return [name for entry in component.get("services") or [] if (name := service_entry_name(entry))]


def _image_tag(image: str | None) -> str:
    """De tag zoals een lezer hem herkent: zonder registry, zonder digest."""
    if not image:
        return "-"
    tail = image.split("@")[0].split("/")[-1]
    return tail if ":" in tail else f"{tail}:latest"


_BARS: tuple[tuple[Literal["mem", "cpu"], str, Callable[[Any], float | None]], ...] = (
    ("mem", "memory", _memory_mi),
    ("cpu", "cpu", _cpu_millicores),
)


def _component_rows(component: dict[str, Any], scale: dict[str, float]) -> list[Row]:
    """Wat een component DOET, los van waar het draait.

    Geen poorten, geen env-vars en geen "achter keycloak": die keuze is met de eigenaar
    gemaakt. Een staafje toont alleen wat het component zelf noemt; de platformstandaard
    die de handler per ontbrekend veld invult is geen feit over dit component.
    """
    rows: list[Row] = []
    declared = _parse_resources_block_partial(component.get("resources"))
    for bar, kind, parse in _BARS:
        request = declared.get(f"requests_{kind}")
        limit = declared.get(f"limits_{kind}")
        if request is None and limit is None:
            continue
        rows.append(
            Row(bar=bar, req=parse(request), lim=parse(limit), max=scale[bar], text=limit or f"{request} (request)")
        )

    health = _component_service_config(component, ServiceType.HEALTH_CHECK)
    if health is not None:
        path = (health or {}).get("liveness-path") if isinstance(health, dict) else None
        rows.append(Row(icon=Glyph.HEALTH, text=path or "health-check"))

    for disk in extract_storage_from_component_services(component):
        temporary = "tijdelijk " if disk.get("type") == "temp" else ""
        rows.append(Row(icon=Glyph.DISK, text=f"{temporary}{disk.get('size')} op {disk.get('mount-path')}"))

    metrics = _component_service_config(component, ServiceType.METRICS_SCRAPER)
    if metrics is not None:
        path = (metrics or {}).get("path") if isinstance(metrics, dict) else None
        rows.append(Row(icon=Glyph.METRICS, text=path or "/metrics"))

    attachments = _component_service_config(component, ServiceType.ATTACHMENTS)
    count = len(attachments) if isinstance(attachments, list) else (1 if attachments else 0)
    if count:
        rows.append(Row(icon=Glyph.CLIP, text=f"{count} {'bijlage' if count == 1 else 'bijlagen'}"))
    return rows


def _resource_scale(components: list[dict[str, Any]]) -> dict[str, float]:
    """De schaal van de staafjes: de hoogste genoemde waarde in het project.

    Een gedeelde schaal maakt de kaarten onderling vergelijkbaar.
    """
    scale = {"mem": 1.0, "cpu": 1.0}
    for component in components:
        declared = _parse_resources_block_partial(component.get("resources"))
        for bar, kind, parse in _BARS:
            for key in (f"requests_{kind}", f"limits_{kind}"):
                scale[bar] = max(scale[bar], parse(declared.get(key)) or 0)
    return scale


def _rules_of(project_data: dict[str, Any], direction: str) -> list[MergedRule]:
    """De cross-domain-regels van een project in EEN richting, project- plus deploymentlaag.

    Dezelfde merge als de generator (``merge_rules`` + ``to_merged_rule``); een onvolledige
    regel wordt overgeslagen, want een kaart hoort niet om te vallen op een halve regel.

    Ontdubbeld: een regel op de projectlaag komt in de merge van ELKE deployment terug, en
    dat zou anders evenveel lijnen naar hetzelfde doel opleveren.
    """
    config = None
    for entry in project_data.get("services") or []:
        if service_entry_name(entry) == ServiceType.CROSS_DOMAIN_ACCESS.value:
            config = service_entry_config(entry)
            break
    if not isinstance(config, dict):
        return []

    rules: dict[MergedRule, None] = {}
    for deployment in [None, *(project_data.get("deployments") or [])]:
        deployment_config: Any = None
        if deployment is not None:
            for entry in deployment.get("services") or []:
                if service_entry_name(entry) == ServiceType.CROSS_DOMAIN_ACCESS.value:
                    deployment_config = service_entry_config(entry)
                    break
            if not isinstance(deployment_config, dict):
                continue
        layer = deployment_config if isinstance(deployment_config, dict) else {}
        merged = merge_rules(direction_rules(config, direction), direction_rules(layer, direction))
        for raw in merged:
            try:
                rule = to_merged_rule(raw, direction=direction)
            except IncompleteRuleError as error:
                logger.warning("kaart: %s", error)
                continue
            if rule is not None:
                rules[rule] = None
    return list(rules)


def incoming_links(project_name: str) -> list[tuple[str, str, str, int]]:
    """Welke projecten wijzen naar DIT project: (van project, van component, naar component, poort).

    De omgekeerde zoekfunctie, die nergens anders bestaat: de regels staan bij de zender en
    bij de ontvanger, en niets geeft "wie wijst naar mij". Een peer ``*`` (een open regel)
    komt terug als projectnaam ``*``.

    Ontdubbeld, want dezelfde regel kan op de projectlaag EN op elke deploymentlaag staan.
    """
    found: dict[tuple[str, str, str, int], None] = {}
    for summary in get_project_store().get_all():
        data = summary.data or {}
        if summary.name == project_name:
            continue
        for rule in _rules_of(data, "outbound"):
            if rule.peer_project != project_name or rule.peer_component is None:
                continue
            found[(summary.name, rule.local_component, rule.peer_component, rule.port)] = None
    # Een eigen open regel (``from: {project: "*"}``) is geen ander project dat wijst, maar
    # hij hoort wel op de kaart: iedereen mag erheen.
    own = get_project_store().get(project_name)
    for rule in _rules_of((own.data or {}) if own else {}, "inbound"):
        if rule.is_open:
            found[(WILDCARD_PROJECT, "", rule.local_component, rule.port)] = None
        elif rule.peer_project and rule.peer_component:
            found[(rule.peer_project, rule.peer_component, rule.local_component, rule.port)] = None
    return list(found)


def _peer_component(graph: Graph, prefix: str, project: str, component: str, column: str) -> str:
    """De knoop van een component van een ANDER project; maakt het kader zo nodig aan.

    ``prefix`` scheidt de inkomende van de uitgaande kant: hetzelfde project kan aan beide
    kanten van de plaat staan, en dan zijn het twee kaders.
    """
    group_id = f"{prefix}:{project}"
    if not any(group.id == group_id for group in graph.groups):
        graph.groups.append(
            Group(id=group_id, title=project, eyebrow="ander project", column=column, pack="elk", style="external")
        )
    node_id = f"{prefix}c:{project}:{component}"
    if not any(node.id == node_id for node in graph.nodes):
        graph.nodes.append(Node(id=node_id, group=group_id, shape="card", color="external", title=component))
    return node_id


def _project_nodes(data: dict[str, Any], project_name: str) -> list[Node]:
    """Kolom 0: het project zelf en het team, inklapbaar naar de leden met hun rol."""
    display_name = data.get("display-name") or project_name
    members = [user for user in (data.get("users") or []) if isinstance(user, dict)]
    return [
        Node(
            id="project",
            column="c0",
            shape="card",
            color="project",
            eyebrow="Project",
            title=display_name,
            subtitle=project_name,
            click="info",
            info=Info(
                eyebrow="Project",
                title=display_name,
                subtitle=project_name,
                sections=[
                    InfoSection(
                        heading="Omschrijving",
                        items=[InfoItem(text=data.get("description") or "Geen omschrijving.")],
                    )
                ],
            ),
        ),
        Node(
            id="team",
            column="c0",
            shape="stack",
            color="team",
            eyebrow="Team",
            title=f"{len(members)} {'lid' if len(members) == 1 else 'leden'}",
            click="expand",
            children=[
                Node(
                    id=f"m:{index}",
                    shape="card",
                    color="team",
                    title=str(user.get("email") or user.get("name") or ""),
                    subtitle=str(user.get("role") or ", ".join(user.get("roles") or [])),
                )
                for index, user in enumerate(members)
            ],
        ),
    ]


def _component_nodes(data: dict[str, Any], per_column: int) -> list[Node]:
    """De componenten, bij meer dan zes verdeeld over kolommen."""
    components = list(data.get("components") or [])
    deployments = list(data.get("deployments") or [])
    scale = _resource_scale(components)
    nodes = []
    for index, component in enumerate(components):
        name = component.get("name") or ""
        used_in = [
            deployment.get("name") or ""
            for deployment in deployments
            if any(entry.get("reference") == name for entry in (deployment.get("components") or []))
        ]
        nodes.append(
            Node(
                id=f"c:{name}",
                column=f"c{1 + index // per_column}",
                shape="card",
                color="component",
                eyebrow=str(component.get("type") or "component"),
                title=name,
                rows=_component_rows(component, scale),
                click="focus",
                info=Info(
                    eyebrow=f"Component · {component.get('type') or 'component'}",
                    title=name,
                    sections=[
                        InfoSection(
                            heading="Services",
                            items=[
                                InfoItem(text=_service_title(service)) for service in _component_services(component)
                            ],
                        ),
                        InfoSection(
                            heading="Draait in",
                            items=[InfoItem(text=deployment_name) for deployment_name in used_in],
                        ),
                    ],
                ),
            )
        )
    return nodes


def _used_service_names(data: dict[str, Any]) -> list[str]:
    """De diensten die het project of een van zijn componenten aanzet, zonder dubbele."""
    names: list[str] = []
    for entry in data.get("services") or []:
        name = service_entry_name(entry)
        if name and name not in names:
            names.append(name)
    for component in data.get("components") or []:
        for name in _component_services(component):
            if name not in names:
                names.append(name)
    return names


def _service_nodes(data: dict[str, Any], column: str) -> list[Node]:
    """De services, als pil in de kleur van hun categorie, met hun uitleg in het paneel."""
    components = list(data.get("components") or [])
    nodes = []
    for name in _used_service_names(data):
        try:
            service_type = ServiceAdapter.get_service_by_value(name)
        except ValueError:
            continue
        definition = SERVICE_DEFINITIONS[service_type]
        users = [component.get("name") or "" for component in components if name in _component_services(component)]
        sections = [
            InfoSection(
                heading="Gebruikt door",
                items=[InfoItem(text=user) for user in users] or [InfoItem(text="het project als geheel")],
            )
        ]
        if definition.help_template:
            try:
                sections.append(InfoSection(html=render_service_help(definition.help_template)))
            except FileNotFoundError:
                logger.warning("kaart: dienst %s wijst naar een uitleg die niet bestaat", name)
        nodes.append(
            Node(
                id=f"s:{name}",
                column=column,
                shape="pill",
                color=_service_color(name),
                title=definition.name,
                subtitle=name,
                click="focus",
                info=Info(eyebrow="Service", title=definition.name, subtitle=name, sections=sections),
            )
        )
    return nodes


def _deployment_nodes(
    data: dict[str, Any], project_name: str, handler: ProjectFileHandler, component_names: set[str | None]
) -> list[Node]:
    """De deployments, elk uitklapbaar naar de componenten die er ECHT in zitten.

    Het webadres komt van publish-on-web; de image-tag van de deployment, want het
    ``image`` in de componentdefinitie wordt bij het uitrollen niet gebruikt.
    """
    nodes = []
    for deployment in data.get("deployments") or []:
        name = deployment.get("name") or ""
        urls = {
            link["component_name"]: link["url"]
            for link in public_urls_for_deployment(data, deployment, project_name, handler)
        }
        children: list[Node] = []
        for entry in deployment.get("components") or []:
            reference = entry.get("reference") or ""
            rows = [Row(icon=Glyph.BOX, text=_image_tag(entry.get("image")))]
            if reference in urls:
                rows.append(Row(icon=Glyph.GLOBE, text=urls[reference].removeprefix("https://"), href=urls[reference]))
            children.append(Node(id=f"di:{name}:{reference}", shape="card", title=reference, rows=rows))
        nodes.append(
            Node(
                id=f"d:{name}",
                group="deps",
                shape="card",
                color="deployment",
                title=name,
                subtitle=str(deployment.get("cluster") or ""),
                click="expand",
                related=[
                    f"c:{entry.get('reference')}"
                    for entry in (deployment.get("components") or [])
                    if entry.get("reference") in component_names
                ],
                children=children,
            )
        )
    return nodes


def _add_link_edges(graph: Graph, data: dict[str, Any], project_name: str, outgoing_column: str) -> None:
    """De cross-domain koppelingen, in beide richtingen, met de poort als label.

    De knopen van het andere project ontstaan hier: een kader per project, met alleen de
    componenten die in een regel genoemd staan.
    """
    local = {node.id for node in graph.nodes}
    for rule in _rules_of(data, "outbound"):
        if rule.peer_project is None or rule.peer_component is None:
            continue
        if f"c:{rule.local_component}" not in local:
            continue
        target = _peer_component(graph, "x", rule.peer_project, rule.peer_component, outgoing_column)
        graph.edges.append(Edge(from_=f"c:{rule.local_component}", to=target, color="link", label=f":{rule.port}"))

    for peer_project, peer_component, local_component, port in incoming_links(project_name):
        if f"c:{local_component}" not in local:
            continue
        if peer_project == WILDCARD_PROJECT:
            if not any(node.id == "x:*" for node in graph.nodes):
                graph.nodes.append(
                    Node(
                        id="x:*",
                        column="c0",
                        shape="hub",
                        color="link",
                        title="Alle projecten",
                        subtitle="cross-domain open",
                    )
                )
            source = "x:*"
        else:
            source = _peer_component(graph, "i", peer_project, peer_component, "c0")
        graph.edges.append(Edge(from_=source, to=f"c:{local_component}", color="link", label=f":{port}"))


def project_graph(project_name: str) -> Graph:
    """De kaart van een project.

    Kolommen van links naar rechts: het project zelf met zijn team en wie naar hem toe
    wijst, dan de componenten, dan de services, dan de deployments, en helemaal rechts de
    projecten waar dit project naartoe wijst.
    """
    summary = get_project_store().get(project_name)
    if summary is None:
        raise KeyError(project_name)
    data = summary.data or {}
    handler = ProjectFileHandler()

    components = list(data.get("components") or [])
    component_names = {component.get("name") for component in components}
    column_count = max(1, math.ceil(len(components) / COMPONENTS_PER_COLUMN))
    per_column = math.ceil(len(components) / column_count) if components else 1
    services_column = f"c{column_count + 1}"
    deployments_column = f"c{column_count + 2}"
    outgoing_column = f"c{column_count + 3}"

    graph = Graph(columns=[Column(id=f"c{index}") for index in range(column_count + 4)])
    if data.get("deployments"):
        graph.groups.append(Group(id="deps", title="Deployments", column=deployments_column, pack="grid"))
    graph.nodes.extend(_project_nodes(data, project_name))
    graph.nodes.extend(_component_nodes(data, per_column))
    graph.nodes.extend(_service_nodes(data, services_column))
    graph.nodes.extend(_deployment_nodes(data, project_name, handler, component_names))

    service_nodes = {node.id for node in graph.nodes}
    for component in components:
        name = component.get("name") or ""
        for service in _component_services(component):
            if f"s:{service}" in service_nodes:
                graph.edges.append(Edge(from_=f"c:{name}", to=f"s:{service}", color=_service_color(service)))
        for other in component.get("uses-components") or []:
            if other in component_names:
                graph.edges.append(Edge(from_=f"c:{name}", to=f"c:{other}", color="component"))

    _add_link_edges(graph, data, project_name, outgoing_column)
    return graph
