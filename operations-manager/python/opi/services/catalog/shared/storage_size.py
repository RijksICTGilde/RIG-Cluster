"""The size of a volume: who chooses it, what it takes, and what applies meanwhile.

Shared by ``persistent-storage`` and ``temp-storage``: up to 2Gi a project chooses; above
it, a platform ceiling for the service (one decision covering all volumes). A deployment may
have its own ceiling. Same model as send-email: project value and per-deployment override.

Pure functions on ``project_data``: no managers, no I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from opi.services.catalog.approval import config_value_approval, deployment_value_approval
from opi.services.catalog.shared.storage import (
    DEFAULT_STORAGE_SIZE,
    FREE_MAX_STORAGE_MI,
    FREE_MAX_STORAGE_SIZE,
    SIZE_FIELD,
)
from opi.services.project import Project
from opi.services.services_enums import ServiceType
from opi.utils.k8s_quantity import parse_k8s_memory_to_mi
from opi.utils.naming import generate_storage_name

if TYPE_CHECKING:
    from opi.services.catalog.approval import ConfigValueApproval, DeploymentValueApproval

#: The two services this module serves. Each keeps its own ceiling under its own service
#: entry, so persistent volumes and temporary ones are separate decisions.
STORAGE_SERVICES: Final[tuple[ServiceType, ...]] = (ServiceType.PERSISTENT_STORAGE, ServiceType.TEMP_STORAGE)

#: Heading of a size request on the approvals page.
SIZE_APPROVAL_LABEL: Final[str] = "Opslaggrootte"

#: The size a restore asks for when the component no longer has the volume being restored.
#: What that path has always used; the PVC is being recreated from a snapshot, and there is
#: no component entry left to read a size from.
RESTORE_UNKNOWN_VOLUME_SIZE: Final[str] = "10Gi"


def _mi(size: str) -> float:
    """``size`` in MiB, 0 when it cannot be read.

    Every comparison goes through this: ``5Gi`` and ``5120Mi`` are the same size, and a
    comparison on the text would make them two different requests.
    """
    try:
        return parse_k8s_memory_to_mi(size)
    except ValueError:
        return 0.0


def _larger(left: str, right: str) -> str:
    """Whichever of the two is the larger quantity."""
    return left if _mi(left) >= _mi(right) else right


def _size_of(config: dict[str, Any]) -> str:
    """The ``size`` in a component entry or an override record; empty when there is none."""
    size = config.get(SIZE_FIELD)
    return size.strip() if isinstance(size, str) and size.strip() else ""


def mount_key(entry: dict[str, Any], index: int) -> str:
    """The key that identifies this volume, the same one the render path uses.

    ``generate_storage_name`` over the mount path and the volume's position in the
    component's full storage list, so the override, the PVC name and the clone state all name
    the same volume. The ``name`` field is NOT that key: backup and restore use it, the render
    path does not, and the two can disagree.
    """
    return generate_storage_name(str(entry.get("mount-path") or entry.get("mount_path") or ""), index)


def component_mounts(component: dict[str, Any]) -> list[tuple[str, ServiceType, dict[str, Any]]]:
    """Every volume of ``component`` as (key, service, entry), in render order."""
    # Lazy: project_file_handler pulls in the catalog, so a module-level import would close
    # the cycle. Its own extractor does the same for the same reason.
    from opi.services.project_file_handler import extract_storage_from_component_services
    from opi.services.schema_migration import _STORAGE_TYPE_TO_SERVICE

    mounts: list[tuple[str, ServiceType, dict[str, Any]]] = []
    for index, entry in enumerate(extract_storage_from_component_services(component)):
        service = _STORAGE_TYPE_TO_SERVICE.get(entry.get("type", ""))
        if service is None:
            continue
        mounts.append((mount_key(entry, index), ServiceType(service), entry))
    return mounts


def mount_key_for_name(component: dict[str, Any], service: ServiceType, storage_name: str) -> str | None:
    """The render key of the volume that backup and restore call ``storage_name``, or None.

    Those paths identify a volume by its ``name`` field, falling back to the render key when
    there is none (``persistent_storages``). This translates that name into the key this
    module keys on, so a component with ``name: logs`` on ``mount-path: /var/log`` resolves to
    its own volume instead of to nothing.
    """
    for key, owner, entry in component_mounts(component):
        if owner is service and (str(entry.get("name") or "") or key) == storage_name:
            return key
    return None


def _deployment_names(project_data: dict[str, Any]) -> list[str]:
    return [
        str(deployment["name"])
        for deployment in project_data.get("deployments") or []
        if isinstance(deployment, dict) and deployment.get("name")
    ]


def _component_entry(
    project_data: dict[str, Any], service: ServiceType, component_name: str, key: str
) -> dict[str, Any] | None:
    """The component's storage entry for this volume, or None when it has no such volume."""
    component = Project(project_data).find("components", name=component_name)
    if component is None:
        return None
    return next(
        (entry for found, owner, entry in component_mounts(component) if found == key and owner is service), None
    )


def _component_size(project_data: dict[str, Any], service: ServiceType, component_name: str, key: str) -> str:
    """The size the component asks for, empty when it has no such volume or no size."""
    return _size_of(_component_entry(project_data, service, component_name, key) or {})


def _largest_component_size(project_data: dict[str, Any], service: ServiceType) -> str:
    """The largest size any component asks for this service; empty when none does."""
    largest = ""
    for component in project_data.get("components") or []:
        if not isinstance(component, dict):
            continue
        for _, owner, entry in component_mounts(component):
            size = _size_of(entry)
            if owner is service and size:
                largest = _larger(largest, size)
    return largest


def _project_granted(project_data: dict[str, Any], service: ServiceType) -> str:
    """The largest volume this service may have in this project without a new approval."""
    last = PROJECT_SIZE_APPROVALS[service].last_approved_value(project_data)
    return _larger(FREE_MAX_STORAGE_SIZE, last) if last else FREE_MAX_STORAGE_SIZE


def _requested_project_ceiling(project_data: dict[str, Any], service: ServiceType) -> str:
    """The ceiling an administrator must decide on; empty is no request.

    Staying within what was already allowed asks nothing and returns the last approved
    ceiling, so the block and with it the memory of what was granted stay put.
    """
    largest = _largest_component_size(project_data, service)
    if largest and _mi(largest) > _mi(_project_granted(project_data, service)):
        return largest
    return PROJECT_SIZE_APPROVALS[service].last_approved_value(project_data)


def _ceiling_activity(service: ServiceType, value: str) -> str:
    """What the decision is about, as the subject of a sentence."""
    return f"Opslag tot {value} per volume voor {service.value}"


def _capped_volumes(project_data: dict[str, Any], service: ServiceType, deployment_name: str) -> list[str]:
    """``<volume> op <size>`` for every volume of this deployment rendering below its size."""
    capped: list[str] = []
    for item in _mount_items(project_data, service):
        if item[0] != deployment_name:
            continue
        chosen = _chosen_mount_size(project_data, service, item)
        effective = effective_mount_size(project_data, service, item)
        if chosen and _mi(effective) < _mi(chosen):
            capped.append(f"{item[2]} op {effective}")
    return capped


def _ceiling_consequence(project_data: dict[str, Any], service: ServiceType, deployment: dict[str, Any]) -> str:
    """Which volumes of this deployment render smaller meanwhile."""
    capped = _capped_volumes(project_data, service, str(deployment.get("name") or ""))
    return f"Voorlopig geldt: {', '.join(capped)}." if capped else ""


def _project_ceiling_approval(service: ServiceType) -> ConfigValueApproval:
    """The approval of the ceiling that covers every volume of one storage service."""
    return config_value_approval(
        service,
        field=SIZE_FIELD,
        label=SIZE_APPROVAL_LABEL,
        requested_value=lambda project_data: _requested_project_ceiling(project_data, service),
        activity=lambda value: _ceiling_activity(service, str(value)),
        consequence=lambda project_data, deployment: _ceiling_consequence(project_data, service, deployment),
        subject=lambda project_data, value: (
            f"{_ceiling_activity(service, value)} (nu {_project_granted(project_data, service)})"
        ),
    )


#: One instance per service, built once: the factory closes over the service.
PROJECT_SIZE_APPROVALS: Final[dict[ServiceType, ConfigValueApproval]] = {
    service: _project_ceiling_approval(service) for service in STORAGE_SERVICES
}


# The deployment layer: ONE deployment's own ceiling, for the volumes it overrides.


def _mount_items(project_data: dict[str, Any], service: ServiceType) -> list[tuple[str, str, str]]:
    """Every (deployment, component, volume) this service has in the project.

    The volumes to resolve a size for, not approval subjects: a verdict is one ceiling per
    project and per deployment.
    """
    items: list[tuple[str, str, str]] = []
    components = {c.get("name"): c for c in project_data.get("components") or [] if isinstance(c, dict)}
    for deployment in project_data.get("deployments") or []:
        if not isinstance(deployment, dict):
            continue
        for entry in deployment.get("components") or []:
            if not isinstance(entry, dict):
                continue
            reference = str(entry.get("reference") or "")
            for key, owner, _ in component_mounts(components.get(reference) or {}):
                if owner is service:
                    items.append((str(deployment.get("name") or ""), reference, key))
    return items


def _override_record(
    project_data: dict[str, Any], service: ServiceType, item: tuple[str, str, str]
) -> dict[str, Any] | None:
    """This deployment's per-volume record for the volume, or None."""
    deployment_name, component_name, key = item
    deployment = Project(project_data).find("deployments", name=deployment_name)
    if deployment is None:
        return None
    component = Project(project_data).locate(deployment.get("components") or [], reference=component_name)
    services = component.get("services") if isinstance(component, dict) else None
    records = services.get(service.value) if isinstance(services, dict) else None
    for record in records or []:
        if isinstance(record, dict) and record.get("reference") == key:
            return record
    return None


def _override(project_data: dict[str, Any], service: ServiceType, item: tuple[str, str, str]) -> str:
    """The size this deployment sets itself, empty when it follows the component."""
    config = (_override_record(project_data, service, item) or {}).get("config")
    return _size_of(config) if isinstance(config, dict) else ""


def _largest_override(project_data: dict[str, Any], service: ServiceType, deployment_name: str) -> str:
    """The largest size this deployment sets itself; empty when it overrides nothing."""
    largest = ""
    for item in _mount_items(project_data, service):
        if item[0] == deployment_name:
            largest = _larger(largest, _override(project_data, service, item))
    return largest


def _deployment_granted(project_data: dict[str, Any], service: ServiceType, deployment_name: str) -> str:
    """The largest volume this deployment may have without a new approval.

    The project's ceiling, or higher once a ceiling was approved for THIS deployment. That
    last part is why an approved ceiling keeps standing when the project's goes down later.
    """
    granted = _project_granted(project_data, service)
    last = DEPLOYMENT_SIZE_APPROVALS[service].last_approved_value(project_data, deployment_name)
    return _larger(granted, last) if last else granted


def _requested_deployment_ceiling(project_data: dict[str, Any], service: ServiceType, deployment_name: str) -> str:
    """The ceiling an administrator must decide on for this deployment; empty is no request.

    The bound is the PROJECT's ceiling and not this deployment's own: its verdict stands
    apart, so it keeps standing when the project lowers its ceiling later. An override within
    the project's ceiling asks nothing.
    """
    largest = _largest_override(project_data, service, deployment_name)
    if largest and _mi(largest) > _mi(_project_granted(project_data, service)):
        return largest
    return DEPLOYMENT_SIZE_APPROVALS[service].last_approved_value(project_data, deployment_name)


def _deployment_ceiling_approval(service: ServiceType) -> DeploymentValueApproval:
    """The approval of ONE deployment's own ceiling."""
    return deployment_value_approval(
        service,
        field=SIZE_FIELD,
        label=f"{SIZE_APPROVAL_LABEL} per deployment",
        requested_value=lambda project_data, deployment_name: _requested_deployment_ceiling(
            project_data, service, deployment_name
        ),
        activity=lambda value: _ceiling_activity(service, str(value)),
        consequence=lambda project_data, deployment: _ceiling_consequence(project_data, service, deployment),
        subject=lambda project_data, deployment_name, value: (
            f"{_ceiling_activity(service, value)} in deployment {deployment_name} "
            f"(nu {_deployment_granted(project_data, service, deployment_name)})"
        ),
    )


DEPLOYMENT_SIZE_APPROVALS: Final[dict[ServiceType, DeploymentValueApproval]] = {
    service: _deployment_ceiling_approval(service) for service in STORAGE_SERVICES
}


def _require_volume(project_data: dict[str, Any], service: ServiceType, component: str, key: str) -> None:
    """Raise when the component does not mount this volume.

    A key the component does not have is a programming error: falling back to the default
    size there would answer 100Mi for a volume of 5Gi.

    Raises:
        KeyError: naming the component and the volume.
    """
    if _component_entry(project_data, service, component, key) is None:
        raise KeyError(f"component '{component}' has no {service.value} volume '{key}'")


def _capped(chosen: str, granted: str) -> str:
    """The chosen size, never above what is granted; the default when nothing was chosen.

    An open request therefore never lowers what already held, and a smaller choice applies at
    once.
    """
    if not chosen:
        return DEFAULT_STORAGE_SIZE
    return chosen if _mi(chosen) <= _mi(granted) else granted


def _chosen_mount_size(project_data: dict[str, Any], service: ServiceType, item: tuple[str, str, str]) -> str:
    """The size asked for this volume in this deployment: its override, else the component's."""
    _, component, key = item
    return _override(project_data, service, item) or _component_size(project_data, service, component, key)


def effective_component_size(project_data: dict[str, Any], service: ServiceType, component: str, key: str) -> str:
    """The size this volume has on the project map, where there is no deployment yet."""
    _require_volume(project_data, service, component, key)
    return _capped(_component_size(project_data, service, component, key), _project_granted(project_data, service))


def effective_mount_size(project_data: dict[str, Any], service: ServiceType, item: tuple[str, str, str]) -> str:
    """THE RESOLVER for one volume of one component in one deployment.

    The override when this deployment sets one, else the component's size, capped at this
    deployment's ceiling.
    """
    deployment, component, key = item
    _require_volume(project_data, service, component, key)
    return _capped(
        _chosen_mount_size(project_data, service, item), _deployment_granted(project_data, service, deployment)
    )


def resolve_component_storage(
    project_data: dict[str, Any], deployment_name: str, component_name: str
) -> list[dict[str, Any]]:
    """The volumes of one component in one deployment, each on its EFFECTIVE size.

    The single reader for every caller that needs a size: the PVC, the ``sizeLimit`` of an
    ephemeral volume, the restore and the project map. They do not combine anything
    themselves, so the fallback cannot mean two different things in two places.
    """
    component = Project(project_data).find("components", name=component_name)
    resolved: list[dict[str, Any]] = []
    for key, service, entry in component_mounts(component or {}):
        resolved.append(
            entry | {SIZE_FIELD: effective_mount_size(project_data, service, (deployment_name, component_name, key))}
        )
    return resolved


def effective_persistent_size_by_name(
    project_data: dict[str, Any], deployment_name: str, component_name: str, storage_name: str
) -> str:
    """The effective size of the persistent volume that backup and restore call ``storage_name``.

    The one entry point for those two paths: they identify a volume by its ``name`` field,
    which is not the key the sizes hang on, so the translation lives here instead of in each
    caller. A name no volume carries keeps the size restore has always used for it.
    """
    component = Project(project_data).find("components", name=component_name) or {}
    key = mount_key_for_name(component, ServiceType.PERSISTENT_STORAGE, storage_name)
    if key is None:
        return RESTORE_UNKNOWN_VOLUME_SIZE
    return effective_mount_size(project_data, ServiceType.PERSISTENT_STORAGE, (deployment_name, component_name, key))


def unknown_override_references(project_data: dict[str, Any], service: ServiceType) -> list[str]:
    """Messages for every override of ``service`` that names a volume its component lacks.

    Such an override does nothing and nobody is told, so the API refuses the write with a
    422. Deliberately NOT a rule on the whole file: a component that drops a volume would
    then leave every deployment that overrode it with a file it can no longer save, and a
    stale record is inert anyway -- the resolver only reads volumes the component has.
    """
    errors: list[str] = []
    known = set(_mount_items(project_data, service))
    for deployment in project_data.get("deployments") or []:
        if not isinstance(deployment, dict):
            continue
        for entry in deployment.get("components") or []:
            if not isinstance(entry, dict):
                continue
            services = entry.get("services")
            records = services.get(service.value) if isinstance(services, dict) else None
            for record in records or []:
                if not isinstance(record, dict):
                    continue
                item = (
                    str(deployment.get("name") or ""),
                    str(entry.get("reference") or ""),
                    str(record.get("reference") or ""),
                )
                if item not in known:
                    errors.append(
                        f"deployment '{item[0]}' stelt opslag in voor volume '{item[2]}', "
                        f"maar component '{item[1]}' heeft dat volume niet"
                    )
    return errors


def refused_shrinks(
    previous: dict[str, Any], project_data: dict[str, Any], *, approval_decision: bool = False
) -> list[str]:
    """Messages for every persistent volume whose effective size goes DOWN in this save.

    Kubernetes refuses a shrink, so the save is refused. Exceptions: temp-storage is emptyDir
    and may shrink; a generation change is a new PVC not a shrink; on the approval route, a
    volume shrinking because its ceiling was withdrawn by that verdict is allowed (denying
    approval is how to go back).
    """
    errors: list[str] = []
    service = ServiceType.PERSISTENT_STORAGE
    known_before = set(_mount_items(previous, service))
    for item in _mount_items(project_data, service):
        if item not in known_before:
            continue
        if _generation(previous, service, item) != _generation(project_data, service, item):
            continue
        if approval_decision and _mi(_deployment_granted(project_data, service, item[0])) < _mi(
            _deployment_granted(previous, service, item[0])
        ):
            continue
        before = effective_mount_size(previous, service, item)
        after = effective_mount_size(project_data, service, item)
        if _mi(after) < _mi(before):
            errors.append(
                f"volume '{item[2]}' van component '{item[1]}' in deployment '{item[0]}' gaat van {before} "
                f"naar {after}; een volume kan niet krimpen"
            )
    return errors


def _generation(project_data: dict[str, Any], service: ServiceType, item: tuple[str, str, str]) -> Any:  # noqa: ANN401  # a project-file value
    config = (_override_record(project_data, service, item) or {}).get("config")
    return config.get("generation") if isinstance(config, dict) else None


def migration_ceilings(project_data: dict[str, Any], service: ServiceType) -> tuple[str, dict[str, str]]:
    """What the 2.19 migration has to carry over as approved: the project's ceiling, and the
    ceiling of every deployment that overrides above it.

    The project ceiling is the largest size any component asks for, when that is above the
    free size; a deployment's ceiling is the largest override it sets above that. Both empty
    means there is nothing to carry over.
    """
    largest = _largest_component_size(project_data, service)
    project_ceiling = largest if _mi(largest) > FREE_MAX_STORAGE_MI else ""
    bound = project_ceiling or FREE_MAX_STORAGE_SIZE
    deployments: dict[str, str] = {}
    for name in _deployment_names(project_data):
        override = _largest_override(project_data, service, name)
        if override and _mi(override) > _mi(bound):
            deployments[name] = override
    return project_ceiling, deployments
