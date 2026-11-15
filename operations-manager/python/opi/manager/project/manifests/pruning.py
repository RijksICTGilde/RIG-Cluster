"""Removing manifest files a round did not write, so ArgoCD prunes the resource."""

from __future__ import annotations

import glob
import logging
import os
from typing import Any

from opi.manager.project.manifests.secrets import attachment_secret_prefix
from opi.services import ServiceType
from opi.utils.sops import SOPS_SUFFIX, TO_SOPS_SUFFIX, sops_filenames

logger = logging.getLogger(__name__)


#: Extensions of generated per-component manifests. Only a file ending in one of these is
#: considered for pruning; that covers plain manifests and both SOPS stages of a secret.
_COMPONENT_MANIFEST_EXTENSIONS: tuple[str, ...] = (
    SOPS_SUFFIX,
    TO_SOPS_SUFFIX,
    ".yaml",
    ".yml",
)


def _is_generated(basename: str, generated_files: set[str]) -> bool:
    """Whether a file belongs to the desired state of this run.

    The ``.sops.yaml`` of a secret written this run as ``.to-sops.yaml`` is its previous
    ciphertext, which the skip-if-unchanged compares against after the prune.
    """
    if basename in generated_files:
        return True
    if basename.endswith(SOPS_SUFFIX):
        return sops_filenames(basename).plaintext in generated_files
    return False


def select_obsolete_component_manifests(
    directory: str,
    component_names: set[str],
    generated_files: set[str],
    deployment_name: str,
) -> list[str]:
    """Select per-component manifest files that this run did not (re)generate.

    Per-component manifests are named with the component reference as the filename
    prefix (e.g. ``<component>-deployment.yaml``, ``<component>-service.yaml``,
    ``<component>-platform-secret.yaml``, ``<component>-user-secret.sops.yaml``,
    ingress/pvc/configmap variants), and the authorization-wall cookie secret uses
    the deployment-scoped unique name (``<deployment>-<component>-oauth2-cookie-...``).

    A per-component file is obsolete when the current run did not generate it
    (``generated_files`` is the complete set written this run). That covers both a
    removed component (none of its files are regenerated) and a renamed/dropped file of
    a surviving component (e.g. an ingress whose path changed, so its filename changed).
    Shared/deployment-level files (kustomization, decrypt-sops, issuer-*,
    *-network-policy, keycloak/registry secrets, etc.) never start with a component
    prefix and are left alone. ``*.marked-for-deletion.yaml`` PVCs are skipped so their
    data lifecycle stays governed by the reconciliation grace period.

    Args:
        directory: Deployment manifest directory to scan (non-recursive).
        component_names: All component references owning files here (current + previous).
        generated_files: Basenames generated this run (the desired-state set).
        deployment_name: Deployment name (for the unique-name oauth2 cookie prefix).

    Returns:
        Sorted list of basenames to remove.
    """
    if not os.path.isdir(directory):
        return []

    def _owning_component(basename: str) -> str | None:
        """Return the component that a basename belongs to, longest prefix wins.

        Considers both the bare ``<component>-`` prefix and the deployment-scoped
        ``<deployment>-<component>-`` prefix used by the oauth2 cookie secret.
        """
        best: str | None = None
        best_len = -1
        for comp in component_names:
            for prefix in (f"{comp}-", f"{deployment_name}-{comp}-"):
                if basename.startswith(prefix) and len(prefix) > best_len:
                    best = comp
                    best_len = len(prefix)
        return best

    selected: list[str] = []
    for path in glob.glob(os.path.join(directory, "*")):
        if not os.path.isfile(path):
            continue
        basename = os.path.basename(path)
        if not basename.endswith(_COMPONENT_MANIFEST_EXTENSIONS):
            continue
        # PVC manifests for a removed service are renamed to *.marked-for-deletion.yaml
        # by pvc_manager.handle_service_removal so ArgoCD keeps the volume alive until
        # the reconciliation grace period (recoverable if the removal was a mistake).
        # Hard-deleting them here would prune the PVC and its data immediately.
        if basename.endswith(".marked-for-deletion.yaml"):
            continue
        # Generated this run -> part of the desired state, keep it.
        if _is_generated(basename, generated_files):
            continue
        # Only prune files that belong to a component; shared/deployment-level files
        # (no component prefix) are never selected.
        if _owning_component(basename) is not None:
            selected.append(basename)

    return sorted(selected)


def select_obsolete_service_manifests(
    directory: str,
    service_prefixes: set[str],
    generated_files: set[str],
) -> list[str]:
    """Select deployment-wide, service-owned manifest files this run did not (re)generate.

    Symmetric with ``select_obsolete_component_manifests`` but keyed on the service
    prefix ``f"{deployment_name}-{service_type.value}-"`` that every
    ``DeploymentManifestSpec`` filename must carry (RC-15). A service-owned deployment
    manifest has no component prefix, so the component prune never touches it; without
    this, a NetworkPolicy would linger after its service is switched off and keep the
    opening alive. ``*.marked-for-deletion.yaml`` files are left to the reconciliation
    lifecycle.

    Args:
        directory: Deployment manifest directory to scan (non-recursive).
        service_prefixes: One ``f"{deployment_name}-{svc.value}-"`` per ServiceType.
        generated_files: Basenames generated this run (the desired-state set).

    Returns:
        Sorted list of basenames to remove.
    """
    if not os.path.isdir(directory):
        return []
    selected: list[str] = []
    for path in glob.glob(os.path.join(directory, "*")):
        if not os.path.isfile(path):
            continue
        basename = os.path.basename(path)
        if not basename.endswith(_COMPONENT_MANIFEST_EXTENSIONS):
            continue
        if basename.endswith(".marked-for-deletion.yaml"):
            continue
        if _is_generated(basename, generated_files):
            continue
        if any(basename.startswith(prefix) for prefix in service_prefixes):
            selected.append(basename)
    return sorted(selected)


def prune_obsolete_service_manifests(deployment: dict[str, Any], target_path: str, generated_files: list[str]) -> None:
    """Delete deployment-wide, service-owned manifest files this run did not regenerate.

    The counterpart of ``prune_obsolete_component_manifests`` for the RC-15
    deployment-manifest hook: a service (cross-domain-access today) writes files named
    ``<deployment>-<service>-...``. When the service is switched off, its last rule is
    removed, a component scope disappears, or a target project vanishes, the run no
    longer generates that file, and this removes it (ArgoCD then prunes the resource).
    The component prune leaves these alone because they carry no component prefix.
    """
    deployment_name = deployment["name"]
    service_prefixes = {f"{deployment_name}-{service.value}-" for service in ServiceType}
    # The attachment Secrets predate the RC-15 naming; renaming them would recreate every
    # mounted Secret, so their own prefix joins the set instead.
    service_prefixes.add(attachment_secret_prefix(deployment_name))
    obsolete = select_obsolete_service_manifests(target_path, service_prefixes, set(generated_files))
    if not obsolete:
        return
    logger.info(f"Pruning {len(obsolete)} obsolete service manifest(s) from {deployment_name}: {', '.join(obsolete)}")
    for basename in obsolete:
        os.remove(os.path.join(target_path, basename))


def prune_obsolete_project_manifests(target_path: str, generated_files: list[str]) -> None:
    """Remove project-wide service files this run did not regenerate.

    The same shape as the prune at deployment level, one layer up.
    """
    service_prefixes = {f"{service.value}-" for service in ServiceType}
    obsolete = select_obsolete_service_manifests(target_path, service_prefixes, set(generated_files))
    if not obsolete:
        return
    logger.info(f"Pruning {len(obsolete)} obsolete project manifest(s): {', '.join(obsolete)}")
    for basename in obsolete:
        os.remove(os.path.join(target_path, basename))
