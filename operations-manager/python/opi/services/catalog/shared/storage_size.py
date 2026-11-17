"""The size of a volume: who chooses it, what it takes, and what applies meanwhile.

Shared by ``persistent-storage`` and ``temp-storage``, which have the same rule. Up to and
including ``FREE_MAX_STORAGE_SIZE`` a project sets the size itself; above it a platform
administrator decides, and until then the volume renders on what it was already allowed. Same
shape as the registry quota of image-registries (``image_registries/quota.py``), one layer
deeper: the component sets the size for all its deployments, and a deployment may override it
per volume with a decision of its own.

Pure functions on ``project_data``: no managers, no I/O. The managers and the manifest
generation read only :func:`resolve_component_storage`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from opi.services.catalog.approval import (
    config_item_value_approval,
    deployment_component_item_value_approval,
)
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
    from opi.services.catalog.approval import ConfigItemValueApproval, DeploymentComponentItemValueApproval

#: The two services this module serves. Each keeps its own approval state under its own
#: service entry, so a persistent volume and a temporary one are separate decisions.
STORAGE_SERVICES: Final[tuple[ServiceType, ...]] = (ServiceType.PERSISTENT_STORAGE, ServiceType.TEMP_STORAGE)

#: Separator in the item name of a component-level approval: ``<component>/<volume>``.
#: Neither half can contain a ``/`` (a component name is a DNS label, a volume key is the
#: generated storage name), so the pair is unambiguous.
ITEM_SEPARATOR: Final[str] = "/"

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


def mount_key(entry: dict[str, Any], index: int) -> str:
    """The key that identifies this volume, the same one the render path uses.

    ``generate_storage_name`` over the mount path and the volume's position in the
    component's full storage list, so the override, its approval, the PVC name and the clone
    state all name the same volume. The ``name`` field is NOT that key: backup and restore
    use it, the render path does not, and the two can disagree.
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


def _component_item(component_name: str, key: str) -> str:
    """The identity of one volume of one component on the approvals page."""
    return f"{component_name}{ITEM_SEPARATOR}{key}"


def _component_items(project_data: dict[str, Any], service: ServiceType) -> list[str]:
    """``<component>/<volume>`` for every volume this service mounts in the project."""
    items: list[str] = []
    for component in project_data.get("components") or []:
        if not isinstance(component, dict):
            continue
        name = str(component.get("name") or "")
        items += [_component_item(name, key) for key, owner, _ in component_mounts(component) if owner is service]
    return items


def _entry_of(project_data: dict[str, Any], service: ServiceType, item: str) -> dict[str, Any] | None:
    """The component's storage entry behind an ``<component>/<volume>`` item."""
    component_name, _, key = item.partition(ITEM_SEPARATOR)
    component = Project(project_data).find("components", name=component_name)
    if component is None:
        return None
    return next(
        (entry for found, owner, entry in component_mounts(component) if found == key and owner is service), None
    )


def _chosen(project_data: dict[str, Any], service: ServiceType, item: str) -> str | None:
    """The size the component asks for, or None when there is no such volume or no size."""
    entry = _entry_of(project_data, service, item)
    size = entry.get(SIZE_FIELD) if entry else None
    return size.strip() if isinstance(size, str) and size.strip() else None


def _granted(project_data: dict[str, Any], service: ServiceType, item: str) -> str:
    """The largest size this volume may use without a new approval."""
    last = COMPONENT_SIZE_APPROVALS[service].last_approved_value(project_data, item)
    return _larger(FREE_MAX_STORAGE_SIZE, last) if last else FREE_MAX_STORAGE_SIZE


def _requested(project_data: dict[str, Any], service: ServiceType, item: str) -> str:
    """The size an administrator must decide on; empty is no request.

    Staying within what was already allowed asks nothing and returns the last approved size,
    so the block and with it the memory of what was granted stay put.
    """
    chosen = _chosen(project_data, service, item)
    if chosen is None:
        return ""
    if _mi(chosen) > _mi(_granted(project_data, service, item)):
        return chosen
    return COMPONENT_SIZE_APPROVALS[service].last_approved_value(project_data, item)


def effective_component_size(project_data: dict[str, Any], service: ServiceType, component: str, key: str) -> str:
    """The size this volume has for every deployment that inherits it.

    The chosen size once it is approved, otherwise the smaller of the chosen size and what
    was already allowed, so an open request never lowers what already held. A key the
    component does not mount is a programming error and raises: falling back to the default
    size there would answer 100Mi for a volume of 5Gi.
    """
    item = _component_item(component, key)
    if _entry_of(project_data, service, item) is None:
        raise KeyError(f"component '{component}' has no {service.value} volume '{key}'")
    chosen = _chosen(project_data, service, item)
    if chosen is None:
        return DEFAULT_STORAGE_SIZE
    if COMPONENT_SIZE_APPROVALS[service].is_approved(project_data, item):
        return chosen
    granted = _granted(project_data, service, item)
    return chosen if _mi(chosen) <= _mi(granted) else granted


def _component_consequence(project_data: dict[str, Any], service: ServiceType, item: str) -> str:
    component, _, key = item.partition(ITEM_SEPARATOR)
    return f"Het volume staat voorlopig op {effective_component_size(project_data, service, component, key)}."


def _component_subject(project_data: dict[str, Any], service: ServiceType, item: str, value: str) -> str:
    component, _, key = item.partition(ITEM_SEPARATOR)
    current = effective_component_size(project_data, service, component, key)
    return f"{value} voor volume {key} van component {component} (nu {current})"


def _component_size_approval(service: ServiceType) -> ConfigItemValueApproval:
    """The approval of the component's size for every volume of one storage service.

    One request per volume of the component, not one per deployment: a deployment that
    inherits is covered by this decision.
    """
    return config_item_value_approval(
        service,
        field=SIZE_FIELD,
        label=SIZE_APPROVAL_LABEL,
        item_names=lambda project_data: _component_items(project_data, service),
        requested_value=lambda project_data, item: _requested(project_data, service, item),
        activity=lambda item, value: f"Een opslaggrootte van {value} voor {item}",
        consequence=lambda project_data, item: _component_consequence(project_data, service, item),
        subject=lambda project_data, item, value: _component_subject(project_data, service, item, value),
    )


#: One instance per service, built once: the factory closes over the service.
COMPONENT_SIZE_APPROVALS: Final[dict[ServiceType, ConfigItemValueApproval]] = {
    service: _component_size_approval(service) for service in STORAGE_SERVICES
}


# The deployment-component layer: ONE deployment's own size for ONE volume.


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


def _override(project_data: dict[str, Any], service: ServiceType, item: tuple[str, str, str]) -> str | None:
    """The size this deployment sets itself, or None when it inherits the component."""
    config = (_override_record(project_data, service, item) or {}).get("config")
    size = config.get(SIZE_FIELD) if isinstance(config, dict) else None
    return size.strip() if isinstance(size, str) and size.strip() else None


def _override_items(project_data: dict[str, Any], service: ServiceType) -> list[tuple[str, str, str]]:
    """Every (deployment, component, volume) this service has in the project."""
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


def _override_granted(project_data: dict[str, Any], service: ServiceType, item: tuple[str, str, str]) -> str:
    """The largest size this deployment may set for this volume without a new approval.

    The largest of three: the free maximum, the component's effective size, and an override
    approved earlier for THIS deployment. The last of those is why an approved override keeps
    standing when the component's own size goes down later.
    """
    _, component, key = item
    granted = _larger(FREE_MAX_STORAGE_SIZE, effective_component_size(project_data, service, component, key))
    last = OVERRIDE_SIZE_APPROVALS[service].last_approved_value(project_data, item)
    return _larger(granted, last) if last else granted


def _override_requested(project_data: dict[str, Any], service: ServiceType, item: tuple[str, str, str]) -> str:
    """The override an administrator must decide on; empty is no request."""
    override = _override(project_data, service, item)
    if override is None:
        return ""
    if _mi(override) > _mi(_override_granted(project_data, service, item)):
        return override
    return OVERRIDE_SIZE_APPROVALS[service].last_approved_value(project_data, item)


def effective_mount_size(project_data: dict[str, Any], service: ServiceType, item: tuple[str, str, str]) -> str:
    """THE RESOLVER for one volume of one component in one deployment.

    Three cases: no override, so the component's effective size; an override within what this
    deployment was already allowed, or approved for exactly that size, so the override; and
    otherwise what it was already allowed, because an open request never lowers what held.
    """
    _, component, key = item
    override = _override(project_data, service, item)
    if override is None:
        return effective_component_size(project_data, service, component, key)
    granted = _override_granted(project_data, service, item)
    if _mi(override) <= _mi(granted) or OVERRIDE_SIZE_APPROVALS[service].is_approved(project_data, item):
        return override
    return granted


def _override_consequence(project_data: dict[str, Any], service: ServiceType, item: tuple[str, str, str]) -> str:
    deployment, _, key = item
    size = effective_mount_size(project_data, service, item)
    return f"Volume {key} staat voor deployment {deployment} voorlopig op {size}."


def _override_subject(
    project_data: dict[str, Any], service: ServiceType, item: tuple[str, str, str], value: str
) -> str:
    deployment, component, key = item
    current = effective_mount_size(project_data, service, item)
    return f"{value} voor volume {key} van component {component} in deployment {deployment} (nu {current})"


def _override_size_approval(service: ServiceType) -> DeploymentComponentItemValueApproval:
    """The approval of ONE deployment's override, per volume."""
    return deployment_component_item_value_approval(
        service,
        field=SIZE_FIELD,
        label=f"{SIZE_APPROVAL_LABEL} per deployment",
        items=lambda project_data: _override_items(project_data, service),
        requested_value=lambda project_data, item: _override_requested(project_data, service, item),
        activity=lambda item, value: f"Een opslaggrootte van {value} voor volume {item[2]} van {item[1]}",
        consequence=lambda project_data, item: _override_consequence(project_data, service, item),
        subject=lambda project_data, item, value: _override_subject(project_data, service, item, value),
    )


OVERRIDE_SIZE_APPROVALS: Final[dict[ServiceType, DeploymentComponentItemValueApproval]] = {
    service: _override_size_approval(service) for service in STORAGE_SERVICES
}


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
    known = set(_override_items(project_data, service))
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

    Kubernetes refuses a smaller PVC, and the sync of the whole deployment fails on it, so the
    save is refused instead. ``temp-storage`` is an ``emptyDir`` and may shrink. A new
    generation of a volume (a recreate through the storage actions) is a new PVC rather than a
    shrink of the one standing, so a changed generation is not judged.

    The rule bounds what a PROJECT may change, so it yields on the approval route for a volume
    whose allowance was withdrawn by that very verdict: denying a standing approval is the way
    back, and refusing the drop it causes would leave the approver unable to take a decision
    back. Only that drop is exempt, so a shrink the verdict does not explain is still refused.
    """
    errors: list[str] = []
    service = ServiceType.PERSISTENT_STORAGE
    for item in _override_items(project_data, service):
        if item not in _override_items(previous, service):
            continue
        if _generation(previous, service, item) != _generation(project_data, service, item):
            continue
        if approval_decision and _mi(_allowance(project_data, service, item)) < _mi(
            _allowance(previous, service, item)
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


def _allowance(project_data: dict[str, Any], service: ServiceType, item: tuple[str, str, str]) -> str:
    """The largest size this volume is allowed by DECISION alone, free maximum included.

    What ``_override_granted`` answers minus the chosen sizes: the free maximum, the last
    approval of the component volume and the last approval of this deployment's override. So a
    drop in it is a withdrawn decision and never a project lowering its own size.
    """
    _, component, key = item
    allowed = _granted(project_data, service, _component_item(component, key))
    last = OVERRIDE_SIZE_APPROVALS[service].last_approved_value(project_data, item)
    return _larger(allowed, last) if last else allowed


def _generation(project_data: dict[str, Any], service: ServiceType, item: tuple[str, str, str]) -> Any:  # noqa: ANN401  # a project-file value
    config = (_override_record(project_data, service, item) or {}).get("config")
    return config.get("generation") if isinstance(config, dict) else None


def migration_items(project_data: dict[str, Any], service: ServiceType) -> list[tuple[str | tuple[str, str, str], str]]:
    """What the 2.19 migration has to carry over as approved, with the size to approve.

    Every component volume above the free size, and every deployment override above what that
    deployment would be allowed without it. The item is the component item name for the first
    and the (deployment, component, volume) triple for the second, so the caller picks the
    matching approval without re-deriving which layer a volume sits on.
    """
    items: list[tuple[str | tuple[str, str, str], str]] = []
    for item in _component_items(project_data, service):
        chosen = _chosen(project_data, service, item)
        if chosen is not None and _mi(chosen) > FREE_MAX_STORAGE_MI:
            items.append((item, chosen))
    for triple in _override_items(project_data, service):
        override = _override(project_data, service, triple)
        if override is not None and _mi(override) > _mi(_override_granted(project_data, service, triple)):
            items.append((triple, override))
    return items
