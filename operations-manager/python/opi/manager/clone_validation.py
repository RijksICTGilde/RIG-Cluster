"""Pre-flight validation for a deployment's ``clone-from`` configuration.

``POST /api/projects/{p}/deployments/{d}/:validate-clone`` answers one question:
would a clone of this deployment have everything it needs? It answers it without
cloning anything, which is the point - a clone writes into a live database or
bucket, so a caller wants to know beforehand.

The checks are on the project file only. Everything a clone needs to be *possible*
is declared there: which kind of source it is, whether that source exists, and
whether the source carries the configuration its kind requires. Reachability of a
remote host is deliberately not checked here; that needs a tunnel and credentials,
and a check that opens connections is no longer a dry run.

This lives apart from ``ProjectManager`` because it is a pure function of the
project data: no git, no cluster, no connectors, and therefore testable as data in,
report out.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.services.services_enums import CloneFromType

logger = logging.getLogger(__name__)

# Kinds of resource a backup clone can carry, as recorded in ``backup_items``.
_KNOWN_BACKUP_RESOURCE_TYPES = ("database", "bucket", "pvc")


def _check(name: str, passed: bool, message: str) -> dict[str, str]:
    return {"name": name, "status": "success" if passed else "failed", "message": message}


def _report(checks: list[dict[str, str]]) -> dict[str, Any]:
    passed = all(check["status"] == "success" for check in checks)
    return {"validation": {"passed": passed, "checks": checks}}


def _validate_deployment_source(project_data: dict[str, Any], reference: str, deployment_name: str) -> dict[str, str]:
    """The source is another deployment in this same project."""
    if not reference:
        return _check("source_deployment_exists", False, "clone-from type 'deployment' without a reference")
    if reference == deployment_name:
        return _check("source_deployment_exists", False, f"Deployment '{deployment_name}' clones from itself")

    names = {dep.get("name") for dep in project_data.get("deployments", []) if isinstance(dep, dict)}
    if reference not in names:
        return _check(
            "source_deployment_exists",
            False,
            f"Source deployment '{reference}' not found in project",
        )
    return _check("source_deployment_exists", True, f"Source deployment '{reference}' found")


def _validate_remote_source(project_data: dict[str, Any], reference: str) -> list[dict[str, str]]:
    """The source is a remote-source entry: an external system reached over a tunnel."""
    if not reference:
        return [_check("remote_source_exists", False, "clone-from type 'remote-source' without a reference")]

    remote_source: dict[str, Any] | None = None
    for source in project_data.get("remote-sources", []):
        if isinstance(source, dict) and source.get("name") == reference:
            remote_source = source
            break

    if remote_source is None:
        return [_check("remote_source_exists", False, f"Remote source '{reference}' not found in project")]

    checks = [_check("remote_source_exists", True, f"Remote source '{reference}' found")]

    chisel = remote_source.get("chisel")
    if isinstance(chisel, dict) and chisel.get("server-url"):
        checks.append(_check("chisel_configuration", True, f"Chisel server: {chisel['server-url']}"))
    else:
        checks.append(_check("chisel_configuration", False, f"Remote source '{reference}' has no chisel server-url"))

    services = remote_source.get("services")
    if isinstance(services, dict) and services:
        checks.append(_check("services_configuration", True, f"Services configured: {', '.join(sorted(services))}"))
    else:
        checks.append(_check("services_configuration", False, f"Remote source '{reference}' configures no services"))

    return checks


def _validate_backup_source(clone_from: dict[str, Any]) -> list[dict[str, str]]:
    """The source is a backup: the items to restore are named in the clone-from itself."""
    items = clone_from.get("backup_items")
    if not isinstance(items, list) or not items:
        return [_check("backup_items", False, "clone-from type 'backup' without backup_items")]

    incomplete = [
        item
        for item in items
        if not isinstance(item, dict)
        or item.get("resource_type") not in _KNOWN_BACKUP_RESOURCE_TYPES
        or not item.get("snapshot_id")
    ]
    if incomplete:
        return [
            _check(
                "backup_items",
                False,
                f"{len(incomplete)} of {len(items)} backup item(s) miss a known resource_type or a snapshot_id",
            )
        ]

    kinds = ", ".join(sorted({str(item["resource_type"]) for item in items}))
    return [_check("backup_items", True, f"{len(items)} backup item(s) to restore: {kinds}")]


def validate_clone_readiness(project_data: dict[str, Any], deployment_name: str) -> dict[str, Any]:
    """Check whether the named deployment's clone configuration is complete.

    Args:
        project_data: The project file contents.
        deployment_name: The deployment whose ``clone-from`` is validated.

    Returns:
        ``{"validation": {"passed": bool, "checks": [{"name", "status", "message"}]}}``
    """
    deployment: dict[str, Any] | None = None
    for candidate in project_data.get("deployments", []):
        if isinstance(candidate, dict) and candidate.get("name") == deployment_name:
            deployment = candidate
            break

    if deployment is None:
        return _report([_check("deployment_exists", False, f"Deployment '{deployment_name}' not found in project")])

    clone_from = deployment.get("clone-from")
    if not clone_from:
        return _report(
            [_check("clone_configuration", False, f"Deployment '{deployment_name}' has no clone-from configuration")]
        )
    if not isinstance(clone_from, dict):
        return _report(
            [
                _check(
                    "clone_configuration",
                    False,
                    "clone-from must be a mapping with 'type', 'reference' and 'mode'",
                )
            ]
        )

    clone_type = clone_from.get("type")
    mode = clone_from.get("mode", "once")
    checks = [_check("clone_configuration", True, f"Clone configuration found: type={clone_type}, mode={mode}")]

    # Whether a clone would actually run. A completed 'once' clone is a valid,
    # finished state - not a failure - so this is reported, not counted against.
    status = clone_from.get("status")
    completed = bool(status.get("completed")) if isinstance(status, dict) else False
    if mode == "once" and completed:
        checks.append(
            _check(
                "clone_pending",
                True,
                "This clone already ran (mode 'once'); a new clone needs force-clone",
            )
        )

    reference = str(clone_from.get("reference") or "")
    if clone_type == CloneFromType.DEPLOYMENT.value:
        checks.append(_validate_deployment_source(project_data, reference, deployment_name))
    elif clone_type == CloneFromType.REMOTE_SOURCE.value:
        checks.extend(_validate_remote_source(project_data, reference))
    elif clone_type == CloneFromType.BACKUP.value:
        checks.extend(_validate_backup_source(clone_from))
    else:
        known = ", ".join(sorted(item.value for item in CloneFromType))
        checks.append(_check("clone_type", False, f"Unknown clone-from type '{clone_type}'. Known types: {known}"))

    return _report(checks)
