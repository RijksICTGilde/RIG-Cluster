"""Resolve cross-domain rules to NetworkPolicy peer selectors (RC-15, pure function).

``resolve_rules`` turns direction-independent ``MergedRule``s into ``ResolvedRule``s that
name a namespace and a pod selector. ``lookup_project`` is injected (in production
``get_project_store().get(name).data``) so this module needs no store, git or FastAPI and
stays unit-testable. Generation must never fail on a broken reference: every unresolvable
rule is logged and skipped, never raised.

A peer project this instance does not know is NOT such a broken reference (RC-42). The
service is called cross-DOMAIN: the other side may be managed elsewhere, or may simply not
exist yet. Such a rule still produces its policy, on the convention that a project's
namespace is its own name -- see ``_conventional_peer``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opi.core.cluster_config import get_prefixed_namespace
from opi.utils.naming import generate_unique_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from opi.services.catalog.cross_domain_access.merge import MergedRule

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeerSelector:
    """A NetworkPolicy peer: a namespace plus the pod labels that pin it to exactly one
    deployment's component. ``pod_labels`` is always ``{"app": "<deployment>-<component>",
    "project": "<project>"}`` -- the project label closes the gap that two projects can pick
    the same namespace name."""

    namespace: str
    pod_labels: dict[str, str]


@dataclass(frozen=True)
class ResolvedRule:
    """A rule ready to render: my component may talk to/from ``peer`` on ``port``."""

    local_component: str
    peer: PeerSelector
    port: int


def _find_deployment(project_data: dict, deployment_name: str) -> dict | None:
    for deployment in project_data.get("deployments", []) or []:
        if isinstance(deployment, dict) and deployment.get("name") == deployment_name:
            return deployment
    return None


def _component_in_deployment(deployment: dict, component_name: str) -> bool:
    for component in deployment.get("components", []) or []:
        if isinstance(component, dict) and component.get("reference") == component_name:
            return True
    return False


def _key(entry: ResolvedRule) -> tuple:
    """Identity of a resolved rule: dedup key and sort key in one, so both stay in step."""
    return (
        entry.local_component,
        entry.peer.namespace,
        tuple(sorted(entry.peer.pod_labels.items())),
        entry.port,
    )


def _selector(cluster: str, rule: MergedRule, namespace: str) -> PeerSelector:
    return PeerSelector(
        namespace=get_prefixed_namespace(cluster, namespace),
        pod_labels={
            "app": generate_unique_name(rule.peer_deployment, rule.peer_component),
            "project": rule.peer_project,
        },
    )


def _conventional_peer(cluster: str, rule: MergedRule) -> PeerSelector:
    """The peer selector for a project this instance cannot read (RC-42).

    Naming a peer is not granting it anything: a NetworkPolicy peer only says which pods MAY
    talk, and the receiving side decides with its own policy whether it lets that in. So a
    rule pointing at a project this cluster does not know must still produce its policy --
    dropping it would silently turn a declared rule into no rule at all, exactly the case the
    two-project cross-domain story starts from (project A is set up before project B exists).

    Without the peer's project file there is no namespace to read, so the platform convention
    is used: a deployment's namespace is the project's own name (``forms/wizard/save.py`` and
    ``router_wizard`` both default it that way), plus the cluster prefix. The pod labels come
    from the rule itself and are unchanged, which keeps the second gate intact: the peer pod
    must carry ``project: <peer>``, so a namespace that happens to be named after the peer but
    belongs to someone else still matches nothing.
    """
    return _selector(cluster, rule, namespace=rule.peer_project)


def resolve_rules(
    rules: list[MergedRule],
    *,
    cluster: str,
    lookup_project: Callable[[str], dict | None],
) -> list[ResolvedRule]:
    """Resolve each rule's peer to a namespace + pod selector on this cluster.

    Every edge case from the design (a missing deployment/component in a project that IS
    known, a deployment on another cluster) is logged with the rule name and skipped --
    resolution never raises. An unknown peer project is not skipped but resolved by
    convention (``_conventional_peer``). The result is deduplicated and sorted so the render
    is stable.

    A rule may name the resolving project itself. The tenant baseline isolates per
    DEPLOYMENT, not per project, so one deployment reaching another deployment of the same
    project needs a rule exactly as much as reaching someone else's. Such a peer resolves
    along the normal path, against the project's own data.
    """
    resolved: dict[tuple, ResolvedRule] = {}
    for rule in rules:
        project_data = lookup_project(rule.peer_project)
        if project_data is None:
            logger.warning(
                "cross-domain rule '%s': project '%s' is not known on this cluster, resolved by convention",
                rule.name,
                rule.peer_project,
            )
            entry = ResolvedRule(
                local_component=rule.local_component, peer=_conventional_peer(cluster, rule), port=rule.port
            )
            resolved.setdefault(_key(entry), entry)
            continue
        deployment = _find_deployment(project_data, rule.peer_deployment)
        if deployment is None:
            logger.warning(
                "cross-domain rule '%s': deployment '%s' not found in project '%s', skipped",
                rule.name,
                rule.peer_deployment,
                rule.peer_project,
            )
            continue
        if deployment.get("cluster") != cluster:
            logger.warning(
                "cross-domain rule '%s': deployment '%s/%s' is on another cluster, skipped",
                rule.name,
                rule.peer_project,
                rule.peer_deployment,
            )
            continue
        if not _component_in_deployment(deployment, rule.peer_component):
            logger.warning(
                "cross-domain rule '%s': component '%s' not in deployment '%s/%s', skipped",
                rule.name,
                rule.peer_component,
                rule.peer_project,
                rule.peer_deployment,
            )
            continue
        namespace = deployment.get("namespace")
        if not namespace:
            logger.warning(
                "cross-domain rule '%s': deployment '%s/%s' has no namespace, skipped",
                rule.name,
                rule.peer_project,
                rule.peer_deployment,
            )
            continue
        entry = ResolvedRule(
            local_component=rule.local_component, peer=_selector(cluster, rule, namespace), port=rule.port
        )
        resolved.setdefault(_key(entry), entry)

    return sorted(resolved.values(), key=_key)
