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

``composite_name_collision`` is here for the same reason: a pure function of project data
(reading the in-memory cache of other projects). A resource name built from project
+ deployment can be built by more than one pair, so we guard against collisions.

``derive_deployment_from_source`` and ``build_restore_target_deployment`` are here on the
same ground: what a new deployment inherits from an existing one is a pure function of
project data, and the upsert, the restore request and the restore task all need the one
answer.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from opi.services.catalog.publish_on_web.domain_config import (
    DomainSetting,
    clear_domain_name_settings,
    get_domain_setting,
    has_domain_setting,
    pop_domain_setting,
    set_domain_setting,
)
from opi.services.project import Project
from opi.services.registry import deployment_runtime_keys
from opi.services.services_enums import CloneFromType
from opi.utils.naming import (
    SELF_CONTAINED_FORMAT_IDS,
    generate_argocd_application_filename,
    generate_argocd_application_name,
    generate_bucket_name,
    generate_database_name,
    generate_database_schema,
    generate_database_username,
    generate_keycloak_client_id,
    generate_minio_policy_name,
    generate_minio_username,
    generate_project_deployment_prefix,
    generate_redis_key_prefix,
    generate_redis_username,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

#: Naming functions that fuse project and deployment: ``('amt', 'odc-prd-staging')`` and
#: ``('amt-odc-prd', 'staging')`` compose the same names. See features/samengestelde-naambotsing.md.
_COMPOSITE_NAME_BUILDERS = (
    generate_database_name,
    generate_database_username,
    generate_database_schema,
    generate_minio_username,
    generate_minio_policy_name,
    generate_bucket_name,
    generate_redis_username,
    generate_redis_key_prefix,
    generate_keycloak_client_id,
    generate_argocd_application_name,
    generate_argocd_application_filename,
    generate_project_deployment_prefix,
)


def _check(name: str, passed: bool, message: str) -> dict[str, str]:
    return {"name": name, "status": "success" if passed else "failed", "message": message}


def _report(checks: list[dict[str, str]]) -> dict[str, Any]:
    passed = all(check["status"] == "success" for check in checks)
    return {"validation": {"passed": passed, "checks": checks}}


def resolve_clone_source_deployment(
    project_data: dict[str, Any], reference: str, deployment_name: str
) -> dict[str, Any]:
    """The deployment that ``clone-from.reference`` names, as a clone source for ``deployment_name``.

    The provisioning path builds source names from project name + reference, so an
    unchecked reference could compose another project's resource name.

    Raises:
        ValueError: if the reference is missing, names the deployment itself, or names a
            deployment that is not part of this project.
    """
    if not reference:
        raise ValueError("clone-from type 'deployment' without a reference")
    if reference == deployment_name:
        raise ValueError(f"Deployment '{deployment_name}' clones from itself")

    source = Project(project_data).find("deployments", name=reference)
    if source is None:
        raise ValueError(f"Source deployment '{reference}' not found in project")
    return source


def other_project_deployments(project_name: str) -> dict[str, list[str]]:
    """The deployment names of every project but ``project_name``, from the project cache."""
    from opi.services.project_store import get_project_store  # circular import otherwise

    return {
        summary.name: [
            dep_name
            for dep in (summary.data.get("deployments") or [])
            if isinstance(dep, dict) and isinstance(dep_name := dep.get("name"), str)
        ]
        for summary in get_project_store().get_all()
        if summary.name != project_name and summary.data
    }


def composite_name_collision(
    project_name: str, deployment_name: str, other_projects: Mapping[str, Sequence[str]]
) -> str | None:
    """Why this project/deployment pair may not be used, or None when it is free.

    A database, bucket, MinIO user or Keycloak client is named by gluing the project name
    and the deployment name together, so ``amt`` + ``odc-prd-staging`` composes the very
    same names as ``amt-odc-prd`` + ``staging``. Provisioning treats an existing resource
    with that name as its own: it resets the password and hands it over, which takes the
    other project's database away from it. Both pairs are valid on their own, so the only
    place the clash is visible is here, against every other project.

    Args:
        project_name: The project the pair belongs to.
        deployment_name: The deployment name to judge.
        other_projects: Deployment names per project, EXCLUDING ``project_name``
            (see ``other_project_deployments``).
    """
    own_names = [build(project_name, deployment_name) for build in _COMPOSITE_NAME_BUILDERS]
    for other_project, deployments in other_projects.items():
        for other_deployment in deployments:
            for name, build in zip(own_names, _COMPOSITE_NAME_BUILDERS, strict=True):
                if name != build(other_project, other_deployment):
                    continue
                return (
                    f"Project '{project_name}' met deployment '{deployment_name}' levert de naam "
                    f"'{name}' op, en die naam is al van deployment '{other_deployment}' van project "
                    f"'{other_project}'. Twee projecten kunnen die resource niet delen, dus kies een "
                    f"andere naam voor het project of voor de deployment"
                )
    return None


def _validate_deployment_source(project_data: dict[str, Any], reference: str, deployment_name: str) -> dict[str, str]:
    """The source is another deployment in this same project."""
    try:
        resolve_clone_source_deployment(project_data, reference, deployment_name)
    except ValueError as error:
        return _check("source_deployment_exists", False, str(error))
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


def _validate_backup_source(
    project_data: dict[str, Any], clone_from: dict[str, Any], deployment_name: str
) -> list[dict[str, str]]:
    """The source is a backup: the clone-from names the run, and the deployment it was taken from.

    Which snapshots that run holds is runtime state at Kopia, not project data, so this
    dry run checks the reference only; ``backup_run_items`` reports a run that is gone.
    """
    run = clone_from.get("run")
    if not isinstance(run, str) or not run:
        return [_check("backup_run", False, "clone-from type 'backup' without a run")]

    try:
        resolve_clone_source_deployment(project_data, str(clone_from.get("reference") or ""), deployment_name)
    except ValueError as error:
        return [_check("backup_run", False, f"backup run '{run}': {error}")]

    return [_check("backup_run", True, f"Backup run '{run}' of deployment '{clone_from['reference']}' to restore")]


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
        checks.extend(_validate_backup_source(project_data, clone_from, deployment_name))
    else:
        known = ", ".join(sorted(item.value for item in CloneFromType))
        checks.append(_check("clone_type", False, f"Unknown clone-from type '{clone_type}'. Known types: {known}"))

    return _report(checks)


def _inherit_exclusions(include_components: bool) -> set[str]:
    """What a new deployment never inherits from its source.

    ``name`` is its own; ``backup`` is an explicit per-deployment choice (an inherited
    schedule made every PR preview accumulate nightly snapshots); the runtime keys are
    what a service recorded ABOUT the source, so they describe another deployment.
    """
    excluded = {"name", "backup", *deployment_runtime_keys()}
    if not include_components:
        excluded.add("components")
    return excluded


def _inherited_components(source: dict[str, Any]) -> list[dict[str, Any]]:
    """The source's components as a starting point: the image and the env vars, nothing else.

    ``user-env-vars`` travels as ciphertext. The project, and so the AGE key, is the same
    one, so nothing is decrypted here.
    """
    components: list[dict[str, Any]] = []
    for component in source.get("components") or []:
        if not isinstance(component, dict) or not component.get("reference"):
            continue
        inherited: dict[str, Any] = {"reference": component["reference"]}
        for key in ("image", "user-env-vars"):
            if key in component:
                inherited[key] = copy.deepcopy(component[key])
        components.append(inherited)
    return components


def derive_deployment_from_source(
    source: dict[str, Any],
    deployment_name: str,
    domain_settings: Mapping[DomainSetting, str] | None = None,
    *,
    include_components: bool = False,
) -> dict[str, Any]:
    """What a new deployment inherits from an existing one, under its own name.

    Args:
        source: The deployment to inherit from.
        deployment_name: The name of the new deployment.
        domain_settings: The web address the caller asked for; it beats the source's.
        include_components: Also inherit the components (image and ``user-env-vars``).
            The upsert leaves them out because the images come from its request body.
    """
    derived = {
        key: copy.deepcopy(value) for key, value in source.items() if key not in _inherit_exclusions(include_components)
    }
    if include_components:
        derived["components"] = _inherited_components(source)

    # A clone uses its own domain setup; web addresses (hosted in services) are cleared to
    # avoid collisions. The SHAPE stays unless it is name-dependent (SELF_CONTAINED_FORMAT_IDS).
    if get_domain_setting(derived, DomainSetting.DOMAIN_FORMAT) not in SELF_CONTAINED_FORMAT_IDS:
        pop_domain_setting(derived, DomainSetting.DOMAIN_FORMAT)
    clear_domain_name_settings(derived)

    for setting, value in (domain_settings or {}).items():
        set_domain_setting(derived, setting, value)

    # A source subdomain equal to its own name means the components share one hostname and
    # differentiate by path. The new deployment keeps that pattern under its own name.
    source_subdomain = get_domain_setting(source, DomainSetting.SUBDOMAIN)
    if source_subdomain and source_subdomain == source.get("name"):
        set_domain_setting(derived, DomainSetting.SUBDOMAIN, deployment_name)

    return derived


def _merge_wizard_components(
    inherited: list[dict[str, Any]], wizard_components: list[Any] | None
) -> list[dict[str, Any]]:
    """The components the wizard collected, each keeping the source's env vars.

    The wizard owns which components the new deployment has and which image each one runs;
    the source owns the ``user-env-vars`` that belong with those images.
    """
    if not wizard_components:
        return inherited
    env_vars = {
        component["reference"]: component["user-env-vars"] for component in inherited if "user-env-vars" in component
    }
    merged: list[dict[str, Any]] = []
    for component in wizard_components:
        if not isinstance(component, dict) or not component.get("reference"):
            continue
        entry = copy.deepcopy(component)
        if entry["reference"] in env_vars and "user-env-vars" not in entry:
            entry["user-env-vars"] = copy.deepcopy(env_vars[entry["reference"]])
        merged.append(entry)
    return merged


def build_restore_target_deployment(
    project_data: dict[str, Any],
    *,
    target_deployment: str,
    backup_source: str,
    backup_run_id: str,
    config_source: str | None,
    wizard_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """The new deployment a restore writes: inherited config, the wizard's choices, clone-from.

    One builder for both runs: the confirmation request builds it on a copy of the project
    to weigh the rights on it, and the restore task builds it again to save it.

    Args:
        project_data: The project the deployment is added to.
        target_deployment: The name of the new deployment.
        backup_source: The deployment the backup was taken from.
        backup_run_id: The backup run to restore.
        config_source: The deployment whose config is inherited, or None for an empty one.
        wizard_config: What the wizard collected (name, environment, components, web address).

    Raises:
        ValueError: if ``config_source`` or ``backup_source`` is not a deployment of this project.
    """
    wizard = copy.deepcopy(wizard_config) if wizard_config else {}
    wizard.pop("clone-from", None)

    if config_source:
        source = resolve_clone_source_deployment(project_data, config_source, target_deployment)
        domain_settings = {
            setting: get_domain_setting(wizard, setting)
            for setting in DomainSetting
            if has_domain_setting(wizard, setting)
        }
        new_deployment = derive_deployment_from_source(
            source, target_deployment, domain_settings, include_components=True
        )
        # The wizard's own web address already went into the derivation, and its ``services``
        # block holds nothing else, so merging it here would drop the source's other services.
        new_deployment.update({key: value for key, value in wizard.items() if key not in ("services", "components")})
        new_deployment["components"] = _merge_wizard_components(
            new_deployment.get("components") or [], wizard.get("components")
        )
        # Once more, AFTER the derivation: its "subdomain equal to the name" heuristic runs
        # over a requested subdomain, and the restore has a web-address step where the user
        # picks one. The upsert does lose its requested subdomain there, and that stays so.
        for setting, value in domain_settings.items():
            set_domain_setting(new_deployment, setting, value)
    else:
        new_deployment = wizard

    new_deployment["name"] = target_deployment

    # Only the run goes in the file: the managers look its snapshots up themselves, so no
    # runtime Kopia data ends up in the project.
    new_deployment["clone-from"] = {
        "type": CloneFromType.BACKUP.value,
        "reference": backup_source,
        "mode": "once",
        "run": backup_run_id,
    }

    backup_source_deployment = Project(project_data).find("deployments", name=backup_source)
    if backup_source_deployment is None:
        raise ValueError(f"Bron deployment '{backup_source}' niet gevonden in project")

    if not new_deployment.get("cluster"):
        new_deployment["cluster"] = (
            backup_source_deployment.get("cluster") or (project_data.get("clusters") or [None])[0]
        )
    if not new_deployment.get("namespace"):
        new_deployment["namespace"] = backup_source_deployment.get("namespace") or project_data.get("name")
    if not new_deployment.get("repository"):
        new_deployment["repository"] = backup_source_deployment.get("repository") or (
            (project_data.get("repositories") or [{}])[0].get("name", "")
        )

    # Presence, not truth: a subdomain the caller stored explicitly, even as null, is a
    # decision and stays untouched (RC-60).
    if not has_domain_setting(new_deployment, DomainSetting.SUBDOMAIN) and get_domain_setting(
        backup_source_deployment, DomainSetting.SUBDOMAIN, ""
    ):
        set_domain_setting(new_deployment, DomainSetting.SUBDOMAIN, target_deployment)

    return new_deployment


def new_or_changed_clone_sources(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The source deployments of every ``clone-from`` this save adds or changes.

    A clone-from reads the source's database and buckets into the deployment it sits on,
    so whoever sets one is acting on the SOURCE as well. Types ``deployment`` and
    ``backup`` both name a deployment of this project; another type names something that
    is not a deployment, and has nothing to weigh here.
    """
    stored = {
        deployment["name"]: deployment.get("clone-from")
        for deployment in before.get("deployments") or []
        if isinstance(deployment, dict) and deployment.get("name")
    }
    sources: list[str] = []
    for deployment in after.get("deployments") or []:
        if not isinstance(deployment, dict) or not deployment.get("name"):
            continue
        clone_from = deployment.get("clone-from")
        if not isinstance(clone_from, dict) or clone_from == stored.get(deployment["name"]):
            continue
        if clone_from.get("type") not in (CloneFromType.DEPLOYMENT.value, CloneFromType.BACKUP.value):
            continue
        reference = clone_from.get("reference")
        if isinstance(reference, str) and reference and reference not in sources:
            sources.append(reference)
    return sources
