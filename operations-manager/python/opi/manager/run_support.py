"""Shared building blocks for user-launched ephemeral workloads ("runs").

Both the database console and ad-hoc jobs are "runs": a label-tracked bundle of
Kubernetes resources applied in one shot and reaped by TTL/label. This module
holds the shared labels/annotations, the one-shot apply, and the label-scoped
delete, so each kind only implements its own manifests + kind-specific cleanup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.services.catalog.image_registries.manifest_pass import apply_rules_to_document
from opi.services.catalog.image_registries.resolution import build_rules, cluster_rules
from opi.services.project import Project

if TYPE_CHECKING:
    from opi.connectors.kubectl import KubectlConnector

logger = logging.getLogger(__name__)


def find_deployment(project_data: dict[str, Any], deployment_name: str) -> dict[str, Any] | None:
    """Return a deployment dict by name from project data (shared by run managers)."""
    return Project(project_data).find("deployments", name=deployment_name)


# Local Kind images: "local/<name>:<tag>" must be pre-loaded into the cluster and
# never pulled. Same rule as project_manager applies for deployment components.
LOCAL_IMAGE_PREFIX = "local/"


def resolve_image(image: str, default_pull_policy: str = "IfNotPresent") -> tuple[str, str]:
    """Resolve an image reference and its pull policy (the shared `local/` rule).

    'local/<name>:<tag>' -> strip the prefix and never pull (Kind-loaded image);
    otherwise return the image unchanged with the given default pull policy. This
    is the single implementation of the local-image special, reused by the
    console, jobs and deployment components so they cannot drift.
    """
    image = image.strip()
    if image.startswith(LOCAL_IMAGE_PREFIX):
        return image[len(LOCAL_IMAGE_PREFIX) :], "Never"
    return image, default_pull_policy


# Labels stamped on every resource of a run bundle. The reaper discovers and
# tears bundles down by these, regardless of kind.
LABEL_RUN = "rig.zad/run"  # value = session id (the bundle key)
LABEL_RUN_KIND = "rig.zad/run-kind"  # value = RunKind (db-console | job)
LABEL_RUN_PROJECT = "rig.zad/run-project"
LABEL_RUN_DEPLOYMENT = "rig.zad/run-deployment"

# Annotations shared by all kinds (kind-specific ones live in each manager).
ANNOT_EXPIRES = "rig.zad/expires-at"
ANNOT_OPENED_BY = "rig.zad/opened-by"
ANNOT_HOSTNAME = "rig.zad/console-hostname"

# Every k8s kind a run bundle can contain on every cluster; the label-scoped delete targets
# all of them so one command removes any bundle (extra kinds simply match nothing).
BUNDLE_KIND_LIST: tuple[str, ...] = ("pod", "service", "ingress", "secret", "configmap")
BUNDLE_KINDS = ",".join(BUNDLE_KIND_LIST)

#: The kind a bundle publishes with in gateway routing. Swept whatever the cluster is
#: configured for, so a console started before a switch is torn down as well.
HTTPROUTE_KIND = "HTTPRoute.gateway.networking.k8s.io"


async def bundle_kinds(kubectl: KubectlConnector, *, exclude: frozenset[str] = frozenset()) -> str:
    """The bundle kinds for a label-scoped query, plus HTTPRoute where the cluster serves it.

    HTTPRoute is not unconditional: naming a type the cluster does not have makes kubectl
    refuse the whole command, and a cluster in ingress routing has no Gateway API.
    """
    kinds = [kind for kind in BUNDLE_KIND_LIST if kind not in exclude]
    httproute = (await kubectl.list_namespaced_kinds() or {}).get(HTTPROUTE_KIND)
    if httproute:
        kinds.append(httproute)
    return ",".join(kinds)


async def apply_bundle(
    kubectl: KubectlConnector,
    namespace: str,
    manifests: list[str],
    cluster: str,
    project_data: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Apply a bundle as one multi-document manifest. Returns (ok, stderr).

    Runs the image-registries pass over each document first - the same rules and the
    same resolve_image() the git deployment pipeline uses - so a directly applied run
    bundle gets its image resolved and its pull secret attached without a second
    mechanism. With ``project_data`` the project's own private registries are part of
    those rules, which is what a job on an image from the project's own registry needs.
    On a cluster with no rules (local/sandbox) the manifests pass through unchanged.
    """
    rules = build_rules(project_data, cluster) if project_data else cluster_rules(cluster)
    docs: list[str] = []
    for manifest in manifests:
        text = manifest.strip()
        if not text:
            continue
        docs.append(apply_rules_to_document(text, rules))

    combined = "\n---\n".join(docs)
    _stdout, stderr, code = await kubectl.run_command(["apply", "-f", "-", "-n", namespace], stdin_input=combined)
    return code == 0, stderr


async def delete_bundle(kubectl: KubectlConnector, namespace: str, session_id: str) -> None:
    """Remove an entire run bundle in one action, by its shared label. Idempotent."""
    selector = f"{LABEL_RUN}={session_id}"
    kinds = await bundle_kinds(kubectl)
    await kubectl.run_command(["delete", kinds, "-l", selector, "-n", namespace, "--ignore-not-found=true"])
