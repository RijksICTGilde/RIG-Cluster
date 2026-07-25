"""Flow definitions for project wizard forms.

Merges the FlowMode/FormFlow dataclasses (from editables/flow.py) and
predefined flow instances (from editables/flows.py) into one module.

Step order is determined by the sections list - the first section
in the list is step 1, the second is step 2, etc. Conditional
sections (e.g., keycloak-config) are included in the list but
only shown when their visibility condition is met.
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
    COMPONENTS_EDIT_SECTION,
    COMPONENTS_SECTION,
    CONFIG_DISPLAY_SECTION,
    DEPLOYMENTS_SECTION,
    IDENTITY_EDIT_SECTION,
    IDENTITY_SECTION,
    KEYCLOAK_CONFIG_SECTION,
    POSTGRESQL_CONFIG_SECTION,
    RESTORE_SELECT_SECTION,
    RESTORE_TARGET_SECTION,
    SERVICES_EDIT_SECTION,
    SERVICES_SECTION,
    TEAM_SECTION,
    build_deployment_wizard_section,
    build_domain_section,
    build_restore_new_deployment_sections,
)
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable
    from opi.forms.visualizers.sections import FormSection


class FlowMode(Enum):
    WIZARD = "wizard"
    TABS = "tabs"


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


CREATE_FLOW = FormFlow(
    flow_id="create-project",
    title="Nieuw project aanmaken",
    mode=FlowMode.WIZARD,
    sections=[
        IDENTITY_SECTION,
        SERVICES_SECTION,
        KEYCLOAK_CONFIG_SECTION,
        POSTGRESQL_CONFIG_SECTION,
        AUTH_WALL_CONFIG_SECTION,
        TEAM_SECTION,
        ATTACHMENTS_SECTION,
        COMPONENTS_SECTION,
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
        KEYCLOAK_CONFIG_SECTION,
        POSTGRESQL_CONFIG_SECTION,
        AUTH_WALL_CONFIG_SECTION,
        TEAM_SECTION,
        COMPONENTS_SECTION,
        DEPLOYMENTS_SECTION,
        CONFIG_DISPLAY_SECTION,
    ],
    show_review=False,
    save_per_section=True,
    htmx_base_url="/forms/wizard",
)


# ---------------------------------------------------------------------------
# Modal edit flows - focused mini-wizards for the detail-page edit modal
# ---------------------------------------------------------------------------

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

MODAL_EDIT_COMPONENTS_FLOW = FormFlow(
    flow_id="modal-edit-components",
    title="Components beheren",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[COMPONENTS_EDIT_SECTION],
)

MODAL_EDIT_SERVICES_FLOW = FormFlow(
    flow_id="modal-edit-services",
    title="Services beheren",
    mode=FlowMode.WIZARD,
    show_review=True,
    sections=[
        SERVICES_EDIT_SECTION,
        KEYCLOAK_CONFIG_SECTION,
        POSTGRESQL_CONFIG_SECTION,
        AUTH_WALL_CONFIG_SECTION,
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

MODAL_EDIT_AUTH_WALL_FLOW = FormFlow(
    flow_id="modal-edit-auth-wall-config",
    title="Authorization wall configuratie",
    mode=FlowMode.WIZARD,
    show_review=False,
    sections=[AUTH_WALL_CONFIG_SECTION],
)

# ---------------------------------------------------------------------------
# Backup & Restore modal flows
# ---------------------------------------------------------------------------

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
    MODAL_EDIT_AUTH_WALL_FLOW.flow_id: MODAL_EDIT_AUTH_WALL_FLOW,
    MODAL_BACKUP_FLOW.flow_id: MODAL_BACKUP_FLOW,
}

# Lookup: service name → modal flow ID (for detail page config buttons)
# service name -> modal-edit flow id, derived from the provider registry
# (modal_flow_id) instead of hand-synced (RC-5 Phase 3).
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

    # When adding a new component, ask which deployments should reference it
    if is_new:
        sections.append(build_component_deployment_select_section(component_index))

    return FormFlow(
        flow_id=f"modal-edit-component-{component_index}",
        title=title,
        mode=FlowMode.WIZARD,
        show_review=True,
        sections=sections,
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
    )


def get_flow(flow_id: str, **context: Any) -> FormFlow:
    """Get a FormFlow by its ID.

    Supports both static registry flows and dynamic domain-edit flows
    (``modal-edit-domain-N`` where N is the deployment index).

    Args:
        flow_id: The flow identifier.
        **context: Extra context forwarded to dynamic flow builders.
            ``component_count`` is used by deployment edit flows.

    Raises:
        KeyError: If the flow_id is not registered.
    """
    if flow_id in FLOW_REGISTRY:
        return FLOW_REGISTRY[flow_id]

    # Restore flow — built dynamically so the new-deployment sections
    # target the correct deployment index (passed via context).
    if flow_id == "modal-restore":
        return build_restore_flow(context.get("deployment_index", 0))

    # Dynamic domain edit flows: modal-edit-domain-0, modal-edit-domain-1, ...
    if flow_id.startswith("modal-edit-domain-"):
        suffix = flow_id.removeprefix("modal-edit-domain-")
        if suffix.isdigit():
            return build_domain_edit_flow(int(suffix))

    # Dynamic component edit flows: modal-edit-component-0, modal-edit-component-1, ...
    if flow_id.startswith("modal-edit-component-"):
        suffix = flow_id.removeprefix("modal-edit-component-")
        if suffix.isdigit():
            return build_component_edit_flow(int(suffix), is_new=context.get("is_new", False))

    # Dynamic deployment edit flows: modal-edit-deployment-0, modal-edit-deployment-1, ...
    if flow_id.startswith("modal-edit-deployment-"):
        suffix = flow_id.removeprefix("modal-edit-deployment-")
        if suffix.isdigit():
            return build_deployment_edit_flow(
                int(suffix),
                component_count=context.get("component_count"),
            )

    # Dynamic backup schedule flows: modal-edit-backup-schedule-0, ...
    if flow_id.startswith("modal-edit-backup-schedule-"):
        suffix = flow_id.removeprefix("modal-edit-backup-schedule-")
        if suffix.isdigit():
            return build_backup_schedule_flow(int(suffix))

    # Dynamic add-deployment flows: modal-add-deployment-0, modal-add-deployment-1, ...
    if flow_id.startswith("modal-add-deployment-"):
        suffix = flow_id.removeprefix("modal-add-deployment-")
        if suffix.isdigit():
            return build_deployment_add_flow(
                int(suffix),
                component_count=context.get("component_count"),
            )

    # Admin domain/subdomain approval flow
    if flow_id == "admin-domain-approval":
        from opi.forms.visualizers.wizard_sections import build_domain_approval_section

        return FormFlow(
            flow_id="admin-domain-approval",
            title="Domein- en subdomeingoedkeuring",
            mode=FlowMode.WIZARD,
            show_review=False,
            sections=[build_domain_approval_section()],
        )

    raise KeyError(f"Unknown flow: {flow_id}")
