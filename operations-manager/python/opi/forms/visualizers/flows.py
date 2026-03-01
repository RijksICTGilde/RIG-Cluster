"""Flow definitions for project wizard forms.

Merges the FlowMode/FormFlow dataclasses (from editables/flow.py) and
predefined flow instances (from editables/flows.py) into one module.

Step order is determined by the sections list — the first section
in the list is step 1, the second is step 2, etc. Conditional
sections (e.g., keycloak-config) are included in the list but
only shown when their visibility condition is met.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from opi.forms.editables.fields.config_generated import GENERATED_EDITABLES_PURE
from opi.forms.visualizers.wizard_sections import (
    AUTH_WALL_CONFIG_SECTION,
    COMPONENTS_SECTION,
    CONFIG_DISPLAY_SECTION,
    DEPLOYMENTS_SECTION,
    DOMAIN_SECTION,
    IDENTITY_SECTION,
    KEYCLOAK_CONFIG_SECTION,
    POSTGRESQL_CONFIG_SECTION,
    SERVICES_SECTION,
    TEAM_SECTION,
    WIZARD_DEPLOYMENT_SECTION,
)

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
    """Editables with generators — computed at submit time, not rendered in forms."""


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
        COMPONENTS_SECTION,
        DOMAIN_SECTION,
        WIZARD_DEPLOYMENT_SECTION,
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

FLOW_REGISTRY: dict[str, FormFlow] = {
    CREATE_FLOW.flow_id: CREATE_FLOW,
    EDIT_FLOW.flow_id: EDIT_FLOW,
}


def get_flow(flow_id: str) -> FormFlow:
    """Get a FormFlow by its ID.

    Raises:
        KeyError: If the flow_id is not registered.
    """
    if flow_id not in FLOW_REGISTRY:
        raise KeyError(f"Unknown flow: {flow_id}")
    return FLOW_REGISTRY[flow_id]
