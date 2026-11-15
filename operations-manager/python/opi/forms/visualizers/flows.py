"""The flow definitions of the project wizard forms.

The sections list is the step order: its first section is step 1. A conditional section
(keycloak-config, for one) is in the list too, and only shown when its condition holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from opi.forms.editables.fields.config_generated import GENERATED_EDITABLES_PURE
from opi.forms.visualizers.wizard_sections import (
    ATTACHMENTS_SECTION,
    AUTH_WALL_CONFIG_SECTION,
    BACKUP_SELECT_SECTION,
    COMPONENTS_CREATE_SECTION,
    COMPONENTS_EDIT_SECTION,
    COMPONENTS_SECTION,
    CONFIG_DISPLAY_SECTION,
    CROSS_DOMAIN_CONFIG_SECTION,
    DEPLOYMENTS_SECTION,
    IDENTITY_EDIT_SECTION,
    IDENTITY_SECTION,
    IMAGE_REGISTRIES_CONFIG_SECTION,
    INVITE_CONFIG_SECTION,
    KEYCLOAK_CONFIG_SECTION,
    MINIO_CONFIG_SECTION,
    POSTGRESQL_CONFIG_SECTION,
    POSTGRESQL_SCHEMAS_SECTION,
    REDIS_CONFIG_SECTION,
    RESTORE_SELECT_SECTION,
    RESTORE_TARGET_SECTION,
    SEAWEEDFS_CONFIG_SECTION,
    SEND_EMAIL_CONFIG_SECTION,
    SERVICES_EDIT_SECTION,
    SERVICES_SECTION,
    SLEEP_MODE_CONFIG_SECTION,
    TEAM_SECTION,
    build_deployment_wizard_section,
    build_domain_section,
    build_restore_new_deployment_sections,
)
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    from collections.abc import Callable

    from opi.forms.editables.editable import Editable
    from opi.forms.visualizers.sections import FormSection


class FlowMode(Enum):
    WIZARD = "wizard"
    TABS = "tabs"


@dataclass(frozen=True)
class FlowTarget:
    """The one list item a flow writes to.

    The index is known the moment the flow is built, which is what makes the flow's sections
    point at ``components[3]`` rather than ``components[*]``. Declared here it stays a number,
    instead of being pushed into the ``flow_id`` string and parsed back out of that text.
    """

    list_key: str
    """Top-level list this flow writes into, e.g. ``components``."""

    index: int
    """Position in that list."""

    is_new: bool = False
    """True when the item does not exist yet and the flow appends it."""


@dataclass(frozen=True)
class ListSelectable:
    """A list in a flow of which the modal may show only part.

    A list modal normally renders every item. With a selection it renders only the
    named ones, and the save splices them back into the stored list, so everything
    outside the selection is neither shown nor submitted and cannot be touched.
    """

    list_path: str
    """Where the list sits, e.g. ``components`` or ``deployments[2]/components``."""

    key_field: str
    """The field that names an item: ``name`` for a component, ``reference`` for its
    use in a deployment."""


@dataclass
class FormFlow:
    """Composes FormSections into a wizard or tabbed interface."""

    flow_id: str
    title: str
    mode: FlowMode
    sections: list[FormSection] = field(default_factory=list)
    show_review: bool = True
    htmx_base_url: str = ""
    save_per_section: bool = True
    generated_editables: list[Editable] = field(default_factory=list)
    """Editables with generators - computed at submit time, not rendered in forms."""
    target: FlowTarget | None = None
    """Where this flow writes, when it edits a single list item.

    ``None`` for flows that write project-wide fields (identity, team,
    service config): those write wherever their editables point.
    """

    selectable: ListSelectable | None = None
    """The list a caller may narrow to a selection, for flows that edit a whole list."""


CREATE_FLOW = FormFlow(
    flow_id="create-project",
    title="Nieuw project aanmaken",
    mode=FlowMode.WIZARD,
    sections=[
        IDENTITY_SECTION,
        SERVICES_SECTION,
        # Before COMPONENTS_SECTION: the registry select on a component reads this list.
        IMAGE_REGISTRIES_CONFIG_SECTION,
        KEYCLOAK_CONFIG_SECTION,
        # After KEYCLOAK_CONFIG_SECTION: the realm-role picker reads the keycloak config
        # (realm-roles plus the authorization-wall role) from the draft of the step before.
        INVITE_CONFIG_SECTION,
        POSTGRESQL_CONFIG_SECTION,
        POSTGRESQL_SCHEMAS_SECTION,
        AUTH_WALL_CONFIG_SECTION,
        TEAM_SECTION,
        ATTACHMENTS_SECTION,
        COMPONENTS_CREATE_SECTION,
        # After COMPONENTS_CREATE_SECTION so the waker-component select is populated from
        # the components already entered into the draft project (empty earlier in the flow).
        SLEEP_MODE_CONFIG_SECTION,
        # After COMPONENTS_SECTION too: the own-component select reads the draft components.
        CROSS_DOMAIN_CONFIG_SECTION,
        REDIS_CONFIG_SECTION,
        MINIO_CONFIG_SECTION,
        SEAWEEDFS_CONFIG_SECTION,
        SEND_EMAIL_CONFIG_SECTION,
        build_deployment_wizard_section(0),
        build_domain_section(0),
    ],
    show_review=True,
    htmx_base_url="/forms/wizard",
    generated_editables=GENERATED_EDITABLES_PURE,
)

EDIT_FLOW = FormFlow(
    flow_id="edit-project",
    title="Project bewerken",
    mode=FlowMode.WIZARD,
    sections=[
        IDENTITY_SECTION,
        SERVICES_SECTION,
        IMAGE_REGISTRIES_CONFIG_SECTION,
        KEYCLOAK_CONFIG_SECTION,
        INVITE_CONFIG_SECTION,
        POSTGRESQL_CONFIG_SECTION,
        POSTGRESQL_SCHEMAS_SECTION,
        AUTH_WALL_CONFIG_SECTION,
        TEAM_SECTION,
        COMPONENTS_SECTION,
        SLEEP_MODE_CONFIG_SECTION,
        CROSS_DOMAIN_CONFIG_SECTION,
        REDIS_CONFIG_SECTION,
        MINIO_CONFIG_SECTION,
        SEAWEEDFS_CONFIG_SECTION,
        SEND_EMAIL_CONFIG_SECTION,
        DEPLOYMENTS_SECTION,
        CONFIG_DISPLAY_SECTION,
    ],
    show_review=False,
    save_per_section=True,
    htmx_base_url="/forms/wizard",
)


MODAL_EDIT_IDENTITY_FLOW = FormFlow(
    flow_id="modal-edit-identity",
    title="Projectgegevens bewerken",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[IDENTITY_EDIT_SECTION],
)

MODAL_EDIT_TEAM_FLOW = FormFlow(
    flow_id="modal-edit-team",
    title="Projectleden beheren",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[TEAM_SECTION],
)

#: The component definitions on the project, the list the components modal works on.
COMPONENTS_SELECTABLE = ListSelectable("components", "name")

MODAL_EDIT_COMPONENTS_FLOW = FormFlow(
    flow_id="modal-edit-components",
    title="Components beheren",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[COMPONENTS_EDIT_SECTION],
    selectable=COMPONENTS_SELECTABLE,
)

MODAL_EDIT_SERVICES_FLOW = FormFlow(
    flow_id="modal-edit-services",
    title="Services beheren",
    mode=FlowMode.WIZARD,
    show_review=True,
    sections=[
        SERVICES_EDIT_SECTION,
        IMAGE_REGISTRIES_CONFIG_SECTION,
        KEYCLOAK_CONFIG_SECTION,
        INVITE_CONFIG_SECTION,
        POSTGRESQL_CONFIG_SECTION,
        POSTGRESQL_SCHEMAS_SECTION,
        AUTH_WALL_CONFIG_SECTION,
        SLEEP_MODE_CONFIG_SECTION,
        CROSS_DOMAIN_CONFIG_SECTION,
        REDIS_CONFIG_SECTION,
        MINIO_CONFIG_SECTION,
        SEAWEEDFS_CONFIG_SECTION,
        SEND_EMAIL_CONFIG_SECTION,
        ATTACHMENTS_SECTION,
    ],
)

MODAL_EDIT_ATTACHMENTS_FLOW = FormFlow(
    flow_id="modal-edit-attachments",
    title="Bijlagen",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[ATTACHMENTS_SECTION],
)

MODAL_EDIT_KEYCLOAK_FLOW = FormFlow(
    flow_id="modal-edit-keycloak-config",
    title="Keycloak configuratie",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[KEYCLOAK_CONFIG_SECTION],
)

MODAL_EDIT_POSTGRESQL_FLOW = FormFlow(
    flow_id="modal-edit-postgresql-config",
    title="Database configuratie",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[POSTGRESQL_CONFIG_SECTION],
)

MODAL_EDIT_POSTGRESQL_SCHEMAS_FLOW = FormFlow(
    flow_id="modal-edit-postgresql-schemas",
    title="Database-schema's",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[POSTGRESQL_SCHEMAS_SECTION],
)

MODAL_EDIT_AUTH_WALL_FLOW = FormFlow(
    flow_id="modal-edit-auth-wall-config",
    title="Authorization wall configuratie",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[AUTH_WALL_CONFIG_SECTION],
)

MODAL_EDIT_SLEEP_MODE_FLOW = FormFlow(
    flow_id="modal-edit-sleep-mode-config",
    title="Slaapstand configuratie",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[SLEEP_MODE_CONFIG_SECTION],
)

MODAL_EDIT_INVITES_FLOW = FormFlow(
    flow_id="modal-edit-invite-config",
    title="Uitnodigingen",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[INVITE_CONFIG_SECTION],
)

MODAL_EDIT_CROSS_DOMAIN_FLOW = FormFlow(
    flow_id="modal-edit-cross-domain-config",
    title="Cross-domain toegang",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[CROSS_DOMAIN_CONFIG_SECTION],
)

MODAL_EDIT_REDIS_FLOW = FormFlow(
    flow_id="modal-edit-redis-config",
    title="Redis configuratie",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[REDIS_CONFIG_SECTION],
)

MODAL_EDIT_MINIO_FLOW = FormFlow(
    flow_id="modal-edit-minio-config",
    title="Objectopslag configuratie",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[MINIO_CONFIG_SECTION],
)

MODAL_EDIT_SEAWEEDFS_FLOW = FormFlow(
    flow_id="modal-edit-seaweedfs-config",
    title="SeaweedFS-objectopslag configuratie",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[SEAWEEDFS_CONFIG_SECTION],
)

MODAL_EDIT_SEND_EMAIL_FLOW = FormFlow(
    flow_id="modal-edit-send-email-config",
    title="E-mail versturen",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[SEND_EMAIL_CONFIG_SECTION],
)

MODAL_EDIT_IMAGE_REGISTRIES_FLOW = FormFlow(
    flow_id="modal-edit-image-registries-config",
    title="Eigen container registries",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[IMAGE_REGISTRIES_CONFIG_SECTION],
)

MODAL_BACKUP_FLOW = FormFlow(
    flow_id="modal-backup",
    title="Backup aanmaken",
    mode=FlowMode.WIZARD,
    show_review=True,
    sections=[BACKUP_SELECT_SECTION],
)


def build_restore_flow(deployment_index: int = 0) -> FormFlow:
    """Build the restore flow with new-deployment sections at the given index."""
    return FormFlow(
        flow_id="modal-restore",
        title="Backup herstellen",
        mode=FlowMode.WIZARD,
        show_review=True,
        sections=[
            RESTORE_SELECT_SECTION,
            RESTORE_TARGET_SECTION,
            *build_restore_new_deployment_sections(deployment_index),
        ],
    )


FLOW_REGISTRY: dict[str, FormFlow] = {
    CREATE_FLOW.flow_id: CREATE_FLOW,
    EDIT_FLOW.flow_id: EDIT_FLOW,
    MODAL_EDIT_IDENTITY_FLOW.flow_id: MODAL_EDIT_IDENTITY_FLOW,
    MODAL_EDIT_TEAM_FLOW.flow_id: MODAL_EDIT_TEAM_FLOW,
    MODAL_EDIT_COMPONENTS_FLOW.flow_id: MODAL_EDIT_COMPONENTS_FLOW,
    MODAL_EDIT_SERVICES_FLOW.flow_id: MODAL_EDIT_SERVICES_FLOW,
    MODAL_EDIT_ATTACHMENTS_FLOW.flow_id: MODAL_EDIT_ATTACHMENTS_FLOW,
    MODAL_EDIT_KEYCLOAK_FLOW.flow_id: MODAL_EDIT_KEYCLOAK_FLOW,
    MODAL_EDIT_POSTGRESQL_FLOW.flow_id: MODAL_EDIT_POSTGRESQL_FLOW,
    MODAL_EDIT_POSTGRESQL_SCHEMAS_FLOW.flow_id: MODAL_EDIT_POSTGRESQL_SCHEMAS_FLOW,
    MODAL_EDIT_AUTH_WALL_FLOW.flow_id: MODAL_EDIT_AUTH_WALL_FLOW,
    MODAL_EDIT_SLEEP_MODE_FLOW.flow_id: MODAL_EDIT_SLEEP_MODE_FLOW,
    MODAL_EDIT_INVITES_FLOW.flow_id: MODAL_EDIT_INVITES_FLOW,
    MODAL_EDIT_CROSS_DOMAIN_FLOW.flow_id: MODAL_EDIT_CROSS_DOMAIN_FLOW,
    MODAL_EDIT_REDIS_FLOW.flow_id: MODAL_EDIT_REDIS_FLOW,
    MODAL_EDIT_MINIO_FLOW.flow_id: MODAL_EDIT_MINIO_FLOW,
    MODAL_EDIT_SEAWEEDFS_FLOW.flow_id: MODAL_EDIT_SEAWEEDFS_FLOW,
    MODAL_EDIT_SEND_EMAIL_FLOW.flow_id: MODAL_EDIT_SEND_EMAIL_FLOW,
    MODAL_EDIT_IMAGE_REGISTRIES_FLOW.flow_id: MODAL_EDIT_IMAGE_REGISTRIES_FLOW,
    MODAL_BACKUP_FLOW.flow_id: MODAL_BACKUP_FLOW,
}

# Service name -> modal-edit flow id, for the config buttons on the detail page. Derived from
# the provider registry (modal_flow_id) rather than hand-synced.
SERVICE_CONFIG_MODAL_FLOWS: dict[str, str] = {
    service_type.value: provider.modal_flow_id
    for service_type in ServiceType
    if (provider := get_service(service_type)).modal_flow_id is not None
}


def build_deployment_edit_flow(
    deployment_index: int,
    component_count: int | None = None,
) -> FormFlow:
    """Build a modal edit flow for a specific deployment's components."""
    from opi.forms.visualizers.wizard_sections import build_deployment_edit_section

    section = build_deployment_edit_section(deployment_index, component_count=component_count)
    return FormFlow(
        flow_id=f"modal-edit-deployment-{deployment_index}",
        title="Deployment bewerken",
        mode=FlowMode.WIZARD,
        show_review=False,
        sections=[section],
        target=FlowTarget("deployments", deployment_index),
        selectable=ListSelectable(f"deployments[{deployment_index}]/components", "reference"),
    )


def build_component_edit_flow(component_index: int, is_new: bool = False) -> FormFlow:
    """Build a modal edit flow for a specific component."""
    from opi.forms.visualizers.wizard_sections import (
        build_component_deployment_select_section,
        build_component_edit_section,
    )

    section = build_component_edit_section(component_index, is_new=is_new)
    title = "Component toevoegen" if is_new else "Component bewerken"
    sections = [section]

    if is_new:
        sections.append(build_component_deployment_select_section(component_index))

    return FormFlow(
        flow_id=f"modal-edit-component-{component_index}",
        title=title,
        mode=FlowMode.WIZARD,
        show_review=True,
        sections=sections,
        target=FlowTarget("components", component_index, is_new=is_new),
    )


def build_deployment_add_flow(
    deployment_index: int,
    component_count: int | None = None,
) -> FormFlow:
    """Build a multi-step wizard for adding a new deployment."""
    from opi.forms.visualizers.wizard_sections import (
        build_deployment_add_components_section,
        build_deployment_add_domain_section,
        build_deployment_add_info_section,
    )

    return FormFlow(
        flow_id=f"modal-add-deployment-{deployment_index}",
        title="Deployment toevoegen",
        mode=FlowMode.WIZARD,
        show_review=True,
        sections=[
            build_deployment_add_info_section(deployment_index),
            build_deployment_add_components_section(deployment_index, component_count),
            build_deployment_add_domain_section(deployment_index),
        ],
        target=FlowTarget("deployments", deployment_index, is_new=True),
    )


def build_backup_schedule_flow(deployment_index: int) -> FormFlow:
    """Build a modal edit flow for a deployment's backup schedule."""
    from opi.forms.visualizers.wizard_sections import build_backup_schedule_section

    section = build_backup_schedule_section(deployment_index)
    return FormFlow(
        flow_id=f"modal-edit-backup-schedule-{deployment_index}",
        title="Backup schema instellen",
        mode=FlowMode.WIZARD,
        show_review=False,
        sections=[section],
        target=FlowTarget("deployments", deployment_index),
    )


def build_cross_domain_deployment_flow(deployment_index: int) -> FormFlow:
    """The per-deployment cross-domain PATCH form.

    One step, owned by the service: which project rule, and which peer deployment it points at
    for THIS deployment. Everything else the rule already says at project level.
    """
    from opi.services.registry import get_service
    from opi.services.services_enums import ServiceType

    section = get_service(ServiceType.CROSS_DOMAIN_ACCESS).deployment_form_section(deployment_index)
    return FormFlow(
        flow_id=f"modal-edit-cross-domain-deployment-{deployment_index}",
        title="Cross-domain toegang per deployment",
        mode=FlowMode.WIZARD,
        show_review=False,
        sections=[section],
        target=FlowTarget("deployments", deployment_index),
    )


def build_postgresql_deployment_flow(deployment_index: int) -> FormFlow:
    """The per-deployment database settings: the connection limit."""
    from opi.services.registry import get_service
    from opi.services.services_enums import ServiceType

    section = get_service(ServiceType.POSTGRESQL_DATABASE).deployment_form_section(deployment_index)
    return FormFlow(
        flow_id=f"modal-edit-postgresql-deployment-{deployment_index}",
        title="Connectielimiet per deployment",
        mode=FlowMode.WIZARD,
        show_review=False,
        sections=[section],
        target=FlowTarget("deployments", deployment_index),
    )


def build_send_email_deployment_flow(deployment_index: int, inherited: dict[str, str] | None = None) -> FormFlow:
    """The sender address and the daily budget of a deployment.

    ``inherited`` holds the PROJECT values, which an empty field here falls back to. They go
    into the field as a placeholder, so whoever opens this form sees what applies now instead
    of an empty box.
    """
    section = get_service(ServiceType.SEND_EMAIL).deployment_form_section(deployment_index, inherited=inherited)
    return FormFlow(
        flow_id=f"modal-edit-send-email-deployment-{deployment_index}",
        title="E-mail per deployment",
        mode=FlowMode.WIZARD,
        show_review=False,
        sections=[section],
        target=FlowTarget("deployments", deployment_index),
    )


def build_minio_deployment_flow(deployment_index: int) -> FormFlow:
    """The limit on old versions of a deployment's bucket."""
    return _build_noncurrent_deployment_flow(
        ServiceType.MINIO_STORAGE, "modal-edit-minio-deployment-", deployment_index
    )


def build_seaweedfs_deployment_flow(deployment_index: int) -> FormFlow:
    """The same limit for a seaweedfs-storage bucket."""
    return _build_noncurrent_deployment_flow(
        ServiceType.SEAWEEDFS_STORAGE, "modal-edit-seaweedfs-deployment-", deployment_index
    )


def _build_noncurrent_deployment_flow(service_type: ServiceType, prefix: str, deployment_index: int) -> FormFlow:
    return FormFlow(
        flow_id=f"{prefix}{deployment_index}",
        title="Oude versies per deployment",
        mode=FlowMode.WIZARD,
        show_review=False,
        sections=[get_service(service_type).deployment_form_section(deployment_index)],
        target=FlowTarget("deployments", deployment_index),
    )


def build_domain_edit_flow(deployment_index: int) -> FormFlow:
    """Build a modal edit flow for a specific deployment's domain config.

    Two steps: the web address itself, then a per-component TLS-mode step where each
    component can override how its certificate is handled.
    """
    from opi.forms.visualizers.wizard_sections import build_domain_cert_section, build_domain_section

    return FormFlow(
        flow_id=f"modal-edit-domain-{deployment_index}",
        title="Webadres bewerken",
        mode=FlowMode.WIZARD,
        show_review=True,
        sections=[
            build_domain_section(deployment_index, edit_mode=True),
            build_domain_cert_section(deployment_index),
        ],
        target=FlowTarget("deployments", deployment_index),
    )


@dataclass(frozen=True)
class IndexedFlow:
    """A family of flows that each edit one item of one list.

    One entry per family, holding everything that would otherwise have to be worked out from
    the flow-id text: which list the family writes to, whether it appends a new item or edits
    an existing one, how to build the flow for an index, and what the builder needs from the
    wizard session (``component_count``, ``is_new``).
    """

    prefix: str
    """Flow-id prefix; the index follows it, e.g. ``modal-edit-component-``."""

    list_key: str
    """Top-level list the family writes into."""

    build: Callable[[int, dict[str, Any]], FormFlow]
    """Build the flow for an index, given the session context."""

    appends_new_item: bool = False
    """True when opening the flow means adding an item (add-deployment)."""

    targets_new_item_when_missing: bool = False
    """True when an index past the end of the list means 'add' (component)."""

    context_from_base: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    """What ``build`` needs from the wizard session's base data."""


def _component_count_context(base_data: dict[str, Any]) -> dict[str, Any]:
    return {"component_count": len(base_data.get("components", []))}


def _send_email_inherited_context(base_data: dict[str, Any]) -> dict[str, Any]:
    """What an empty field in a deployment's send-email step means.

    The EFFECTIVE project values, because that is what a deployment filling in nothing really
    gets: the approved address (empty means the relay derives it) and the budget the gate lets
    through, not the one that was asked for.
    """
    from opi.services.catalog.send_email import approved_from_address, effective_messages_per_day, project_from_name

    return {
        "inherited_send_email": {
            "from-name": project_from_name(base_data),
            "from-address": approved_from_address(base_data),
            "messages-per-day": str(effective_messages_per_day(base_data)),
        }
    }


def _is_new_context(base_data: dict[str, Any]) -> dict[str, Any]:
    return {"is_new": True} if base_data.get("is_new") else {}


INDEXED_FLOWS: tuple[IndexedFlow, ...] = (
    IndexedFlow(
        prefix="modal-edit-component-",
        list_key="components",
        build=lambda index, ctx: build_component_edit_flow(index, is_new=bool(ctx.get("is_new"))),
        targets_new_item_when_missing=True,
        context_from_base=_is_new_context,
    ),
    IndexedFlow(
        prefix="modal-edit-deployment-",
        list_key="deployments",
        build=lambda index, ctx: build_deployment_edit_flow(index, component_count=ctx.get("component_count")),
        context_from_base=_component_count_context,
    ),
    IndexedFlow(
        prefix="modal-add-deployment-",
        list_key="deployments",
        build=lambda index, ctx: build_deployment_add_flow(index, component_count=ctx.get("component_count")),
        appends_new_item=True,
        context_from_base=_component_count_context,
    ),
    IndexedFlow(
        prefix="modal-edit-domain-",
        list_key="deployments",
        build=lambda index, _ctx: build_domain_edit_flow(index),
    ),
    IndexedFlow(
        prefix="modal-edit-backup-schedule-",
        list_key="deployments",
        build=lambda index, _ctx: build_backup_schedule_flow(index),
    ),
    IndexedFlow(
        prefix="modal-edit-cross-domain-deployment-",
        list_key="deployments",
        build=lambda index, _ctx: build_cross_domain_deployment_flow(index),
    ),
    IndexedFlow(
        prefix="modal-edit-postgresql-deployment-",
        list_key="deployments",
        build=lambda index, _ctx: build_postgresql_deployment_flow(index),
    ),
    IndexedFlow(
        prefix="modal-edit-send-email-deployment-",
        list_key="deployments",
        build=lambda index, ctx: build_send_email_deployment_flow(index, ctx.get("inherited_send_email")),
        context_from_base=_send_email_inherited_context,
    ),
    IndexedFlow(
        prefix="modal-edit-minio-deployment-",
        list_key="deployments",
        build=lambda index, _ctx: build_minio_deployment_flow(index),
    ),
    IndexedFlow(
        prefix="modal-edit-seaweedfs-deployment-",
        list_key="deployments",
        build=lambda index, _ctx: build_seaweedfs_deployment_flow(index),
    ),
)


def parse_indexed_flow_id(flow_id: str) -> tuple[IndexedFlow, int] | None:
    """Match a flow id against the indexed-flow families. None if it is not one."""
    for kind in INDEXED_FLOWS:
        if flow_id.startswith(kind.prefix):
            suffix = flow_id.removeprefix(kind.prefix)
            if suffix.isdigit():
                return kind, int(suffix)
            return None
    return None


def flow_context_from_base(flow_id: str, base_data: dict[str, Any] | None) -> dict[str, Any]:
    """What the builder for *flow_id* needs from a wizard session's base data.

    Deployment flows need ``component_count`` so the sequence enforces a
    max-items limit matching the number of project components; component add
    flows need ``is_new`` so the name field stays editable; the restore flow
    needs the index of the empty deployment slot appended at init.
    """
    if not base_data:
        return {}
    if flow_id == "modal-restore":
        # The new deployment index = total deployments - 1 (the appended empty slot)
        return {"deployment_index": len(base_data.get("deployments", [])) - 1}
    match = parse_indexed_flow_id(flow_id)
    if match is None or match[0].context_from_base is None:
        return {}
    return match[0].context_from_base(base_data)


def get_flow(flow_id: str, **context: Any) -> FormFlow:  # noqa: ANN401  # a project-file value
    """Get a FormFlow by its ID.

    Both a static registry flow and an indexed family from ``INDEXED_FLOWS``
    (``modal-edit-domain-N`` and friends, where N is the list index the flow targets).
    ``context`` is forwarded to a dynamic flow builder; a deployment edit flow reads
    ``component_count`` from it. Raises ``KeyError`` for an unregistered flow_id.
    """
    if flow_id in FLOW_REGISTRY:
        return FLOW_REGISTRY[flow_id]

    # The restore flow is built dynamically, so its new-deployment sections target the
    # deployment index the context names.
    if flow_id == "modal-restore":
        return build_restore_flow(context.get("deployment_index", 0))

    match = parse_indexed_flow_id(flow_id)
    if match is not None:
        kind, index = match
        return kind.build(index, context)

    # Admin approval flow: every approval a service declares, not just domains
    if flow_id == "admin-approval":
        from opi.forms.visualizers.wizard_sections import build_domain_approval_section

        return FormFlow(
            flow_id="admin-approval",
            title="Goedkeuring van aanvragen",
            mode=FlowMode.WIZARD,
            show_review=False,
            sections=[build_domain_approval_section()],
        )

    raise KeyError(f"Unknown flow: {flow_id}")
