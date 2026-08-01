"""Resolve cross-domain rules to NetworkPolicy peer selectors (RC-15, pure function).

``resolve_rules`` turns direction-independent ``MergedRule``s into ``ResolvedRule``s that
name a namespace and a pod selector. ``lookup_project`` is injected (in production
``get_project_store().get(name).data``) so this module needs no store, git or FastAPI and
stays unit-testable. Generation must never fail on a broken reference: every unresolvable
rule is logged and skipped, never raised.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from opi.core.cluster_config import get_prefixed_namespace
from opi.services.catalog.cross_domain_access.merge import MergedRule
from opi.utils.naming import generate_unique_name

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


def resolve_rules(
    rules: list[MergedRule],
    *,
    cluster: str,
    self_project: str,
    lookup_project: Callable[[str], dict | None],
) -> list[ResolvedRule]:
    """Resolve each rule's peer to a namespace + pod selector on this cluster.

    Every edge case from the design (self-reference, missing project/deployment/component,
    a deployment on another cluster) is logged with the rule name and skipped -- resolution
    never raises. The result is deduplicated and sorted so the render is stable.
    """
    resolved: dict[tuple, ResolvedRule] = {}
    for rule in rules:
        if rule.peer_project == self_project:
            logger.warning("cross-domain rule '%s': references own project, skipped", rule.name)
            continue
        project_data = lookup_project(rule.peer_project)
        if project_data is None:
            logger.warning(
                "cross-domain rule '%s': project '%s' does not exist, skipped", rule.name, rule.peer_project
            )
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
        selector = PeerSelector(
            namespace=get_prefixed_namespace(cluster, namespace),
            pod_labels={
                "app": generate_unique_name(rule.peer_deployment, rule.peer_component),
                "project": rule.peer_project,
            },
        )
        entry = ResolvedRule(local_component=rule.local_component, peer=selector, port=rule.port)
        key = (entry.local_component, selector.namespace, tuple(sorted(selector.pod_labels.items())), entry.port)
        resolved.setdefault(key, entry)

    return sorted(
        resolved.values(),
        key=lambda r: (r.local_component, r.peer.namespace, tuple(sorted(r.peer.pod_labels.items())), r.port),
    )
