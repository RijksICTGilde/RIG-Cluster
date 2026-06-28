"""Shared building blocks for user-launched ephemeral workloads ("runs").

Both the database console and ad-hoc jobs are "runs": a label-tracked bundle of
Kubernetes resources applied in one shot and reaped by TTL/label. This module
holds the shared labels/annotations, the one-shot apply, and the label-scoped
delete, so each kind only implements its own manifests + kind-specific cleanup.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import yaml

from opi.extensions.pipeline import load_extensions

if TYPE_CHECKING:
    from opi.connectors.kubectl import KubectlConnector

logger = logging.getLogger(__name__)


def find_deployment(project_data: dict[str, Any], deployment_name: str) -> dict[str, Any] | None:
    """Return a deployment dict by name from project data (shared by run managers)."""
    for deployment in project_data.get("deployments", []) or []:
        if deployment.get("name") == deployment_name:
            return deployment
    return None


def parse_expires(value: str | None) -> datetime | None:
    """Parse a stored expires-at / timestamp annotation; None on missing/bad data."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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

# Every k8s kind a run bundle can contain; the label-scoped delete targets all of
# them so one command removes any bundle (extra kinds simply match nothing).
BUNDLE_KINDS = "pod,service,ingress,secret,configmap"


async def apply_bundle(
    kubectl: KubectlConnector, namespace: str, manifests: list[str], cluster: str
) -> tuple[bool, str]:
    """Apply a bundle as one multi-document manifest. Returns (ok, stderr).

    Runs the cluster's manifest extension pipeline (e.g. the ghcr->rcr registry
    rewrite on ODCN) over each document first - the exact same extensions the git
    deployment pipeline uses - so direct-applied run bundles get the same image
    rewriting without duplicating that logic. On clusters with no extensions
    (local/sandbox) the manifests pass through unchanged.
    """
    pipeline = load_extensions(cluster)
    docs: list[str] = []
    for manifest in manifests:
        text = manifest.strip()
        if not text:
            continue
        if pipeline.has_extensions:
            parsed = yaml.safe_load(text)
            if isinstance(parsed, dict):
                text = yaml.safe_dump(pipeline.process_manifest(parsed), default_flow_style=False, sort_keys=False)
        docs.append(text)

    combined = "\n---\n".join(docs)
    _stdout, stderr, code = await kubectl.run_command(["apply", "-f", "-", "-n", namespace], stdin_input=combined)
    return code == 0, stderr


async def delete_bundle(kubectl: KubectlConnector, namespace: str, session_id: str) -> None:
    """Remove an entire run bundle in one action, by its shared label. Idempotent."""
    selector = f"{LABEL_RUN}={session_id}"
    await kubectl.run_command(["delete", BUNDLE_KINDS, "-l", selector, "-n", namespace, "--ignore-not-found=true"])
