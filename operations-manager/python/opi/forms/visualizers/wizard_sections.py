"""Wizard section definitions for project forms.

Each FormSection groups related editables into a logical wizard step.
Step ordering is determined by the sections list in FormFlow, NOT by
any property on the section itself.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from opi.forms.editables.enforcers import (
    ComponentServicesEnforcer,
    DomainConfigEnforcer,
    UniqueDeploymentNameEnforcer,
    UniqueReferencesEnforcer,
    extract_service_names,
)
from opi.forms.layout import DisplayBlock, Fieldset, Sequence, TemplatePartial
from opi.forms.visualizers.display_blocks import compute_url_preview as _compute_url_preview
from opi.forms.visualizers.fields.components import COMPONENTS_SEQUENCE
from opi.forms.visualizers.fields.config_display import AGE_PRIVATE_KEY, AGE_PUBLIC_KEY, API_KEY
from opi.forms.visualizers.fields.deployments import (
    DEPLOYMENT_BASE_DOMAIN,
    DEPLOYMENT_CLONE_FROM,
    DEPLOYMENT_COMP_IMAGE,
    DEPLOYMENT_COMP_REFERENCE,
    DEPLOYMENT_COMP_USER_ENV_VARS,
    DEPLOYMENT_COMPONENTS_SEQ,
    DEPLOYMENT_CUSTOM_BASE_DOMAIN,
    DEPLOYMENT_DOMAIN_FORMAT,
    DEPLOYMENT_NAME,
    DEPLOYMENT_SUBDOMAIN,
    DEPLOYMENTS_SEQUENCE,
)
from opi.forms.visualizers.fields.domains import (
    DOMAIN_CONFIG,
    WIZARD_DEPLOYMENT_NAME,
)
from opi.forms.visualizers.fields.identity import CLUSTERS, DESCRIPTION, DISPLAY_NAME
from opi.forms.visualizers.fields.services import (
    AUTH_WALL_BANNER,
    KEYCLOAK_ADDITIONAL_CLIENTS,
    KEYCLOAK_REDIRECT_URIS,
    KEYCLOAK_RESTRICT_ACCESS,
    KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG,
    KEYCLOAK_RESTRICT_ACCESS_ROLE,
    KEYCLOAK_TEMPLATE,
    POSTGRESQL_INSTANCES,
    POSTGRESQL_STORAGE,
    SERVICES,
)
from opi.forms.visualizers.fields.team import USERS_SEQUENCE
from opi.forms.visualizers.sections import FormSection


def _extract_services(data: dict[str, Any]) -> list[str]:
    """Extract active service names from wizard form data."""
    services = data.get("services", [])
    if isinstance(services, str):
        return [services] if services else []
    if isinstance(services, list):
        return extract_service_names(services)
    return []


# ---------------------------------------------------------------------------
# Core sections (always visible)
# ---------------------------------------------------------------------------

IDENTITY_SECTION = FormSection(
    section_id="identity",
    title="Projectgegevens",
    icon="document-blanco",
    description="Basisinformatie over uw project",
    editables=[DISPLAY_NAME, DESCRIPTION, CLUSTERS],
    layout=[
        "display-name",
        "description",
        "clusters",
    ],
)

SERVICES_SECTION = FormSection(
    section_id="services",
    title="Services",
    icon="applicatie",
    description="Selecteer de services die u wilt activeren",
    editables=[SERVICES],
    layout=["services"],
)

TEAM_SECTION = FormSection(
    section_id="team",
    title="Projectleden",
    icon="groep-3-personen",
    description="Beheer teamleden",
    editables=[USERS_SEQUENCE],
    layout=[Sequence(field_name="users")],
)

COMPONENTS_SECTION = FormSection(
    section_id="components",
    title="Componenten",
    icon="puzzel",
    description="Definieer de applicatiecomponenten",
    enforcer=ComponentServicesEnforcer(),
    editables=[COMPONENTS_SEQUENCE],
    layout=[
        Sequence(
            field_name="components",
            child_layout=[
                Fieldset(
                    legend="Identificatie",
                    children=[
                        "name",
                        "image",
                    ],
                ),
                Fieldset(
                    legend="Resources",
                    description="Geheugen limieten voor dit component. Gebruik de standaardwaarden als je niet zeker weet wat je nodig hebt. Dit kan later aangepast worden. "
                    "Deze waardes zijn een richtlijn, de waardes zullen aangepast worden aan het daadwerkelijke gebruik.",
                    children=[
                        "resources/memory/request",
                        "resources/memory/limit",
                    ],
                ),
                Fieldset(
                    legend="Netwerk",
                    description="Poorten waarop het component luistert voor inkomend verkeer.",
                    children=[
                        Sequence(field_name="ports/inbound"),
                    ],
                ),
                Fieldset(
                    legend="Services",
                    description="Selecteer welke services dit component gebruikt.",
                    children=["services"],
                ),
                Fieldset(
                    legend="Publicatie",
                    description=(
                        "Bij gedeelde domeinen bepaalt het pad welk component het verkeer ontvangt. "
                        "Bijvoorbeeld: / voor de frontend en /api voor de backend."
                    ),
                    children=[
                        "path",
                        "rewrite-path",
                    ],
                ),
                Fieldset(
                    legend="Variabelen",
                    description="Omgevingsvariabelen en aliassen voor dit component.",
                    children=[
                        "aliases",
                        "user-env-vars",
                    ],
                ),
                Sequence(field_name="services{persistent-storage}/config"),
                Sequence(field_name="services{temp-storage}/config"),
                Fieldset(
                    legend="Prometheus metrics scraper configuratie",
                    depends_on="services",
                    show_when={"contains": "metrics-scraper"},
                    children=[
                        "services{metrics-scraper}/port",
                        "services{metrics-scraper}/path",
                    ],
                ),
            ],
        ),
    ],
)

DEPLOYMENTS_SECTION = FormSection(
    section_id="deployments",
    title="Deployments",
    icon="server",
    description="Configureer de deployment-omgevingen",
    editables=[DEPLOYMENTS_SEQUENCE],
    layout=[Sequence(field_name="deployments")],
)

CONFIG_DISPLAY_SECTION = FormSection(
    section_id="config",
    title="Configuratie",
    icon="instellingen",
    description="Automatisch gegenereerde configuratie (alleen-lezen)",
    is_readonly=True,
    editables=[AGE_PUBLIC_KEY, AGE_PRIVATE_KEY, API_KEY],
    layout=[
        "config/age-public-key",
        "config/age-private-key",
        "config/api-key",
    ],
)

# ---------------------------------------------------------------------------
# Conditional sections (visible based on selected services)
# ---------------------------------------------------------------------------

KEYCLOAK_CONFIG_SECTION = FormSection(
    section_id="keycloak-config",
    title="Keycloak configuratie",
    icon="sleutel",
    description="SSO en authenticatie-instellingen",
    visible=lambda data: "keycloak" in _extract_services(data),
    editables=[
        KEYCLOAK_TEMPLATE,
        KEYCLOAK_REDIRECT_URIS,
        KEYCLOAK_RESTRICT_ACCESS,
        KEYCLOAK_RESTRICT_ACCESS_ROLE,
        KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG,
        KEYCLOAK_ADDITIONAL_CLIENTS,
    ],
    layout=[
        Fieldset(
            legend="Template",
            children=["services/keycloak/config/template"],
        ),
        # Hidden: redirect URIs are managed via additional clients instead.
        # Fieldset(
        #     legend="Extra redirect-URI\u2019s",
        #     ...
        #     children=[Sequence(field_name="services/keycloak/config/additional_redirect_uris")],
        # ),
        Fieldset(
            legend="Toegangsbeperking",
            description="Beperk toegang tot de applicatie op basis van Keycloak realm-rollen.",
            children=[
                "services/keycloak/config/restrict-access/enabled",
                "services/keycloak/config/restrict-access/realm-role",
                "services/keycloak/config/restrict-access/error-message",
            ],
        ),
        Fieldset(
            legend="Extra Keycloak clients",
            description=(
                "Voeg extra clients toe als er externe applicaties zijn "
                "die het Keycloak realm van dit project gebruiken. Elke client krijgt een eigen client-ID "
                "en redirect URI's."
            ),
            children=[
                Sequence(field_name="services/keycloak/config/additional-clients"),
            ],
        ),
    ],
)

POSTGRESQL_CONFIG_SECTION = FormSection(
    section_id="postgresql-config",
    title="Database configuratie",
    icon="database",
    description="PostgreSQL database-instellingen",
    visible=lambda data: "namespace-postgresql-database" in _extract_services(data),
    editables=[POSTGRESQL_INSTANCES, POSTGRESQL_STORAGE],
    layout=[
        "services/namespace-postgresql-database/config/instances",
        "services/namespace-postgresql-database/config/storage",
    ],
)

DOMAIN_SECTION = FormSection(
    section_id="domains",
    title="Webadres",
    icon="wereldbol",
    description="Configureer hoe uw applicatie bereikbaar wordt",
    enforcer=DomainConfigEnforcer(),
    editables=[DOMAIN_CONFIG],
    layout=[
        TemplatePartial(template="wizard/partials/domain_info.html.j2"),
        "deployments[0]/base-domain",
        "deployments[0]/base-domain:custom",
        "deployments[0]/domain-format",
        "deployments[0]/subdomain",
        "deployments[0]/root-component",
        DisplayBlock(
            display_id="url-preview",
            compute=_compute_url_preview,
            template="wizard/partials/url_preview.html.j2",
            context={"deployment_index": 0},
        ),
    ],
)

WIZARD_DEPLOYMENT_SECTION = FormSection(
    section_id="deployment",
    title="Deployment",
    icon="server",
    description="Configureer de deployment voor uw applicatie",
    editables=[WIZARD_DEPLOYMENT_NAME],
    layout=[
        TemplatePartial(template="wizard/partials/deployment_info.html.j2"),
        "deployments[0]/name",
    ],
)

AUTH_WALL_CONFIG_SECTION = FormSection(
    section_id="auth-wall-config",
    title="Authorization wall configuratie",
    icon="sleutel",
    description="Instellingen voor de toegangspagina",
    visible=lambda data: "authorization-wall" in _extract_services(data),
    editables=[AUTH_WALL_BANNER],
    layout=["services/authorization-wall/config/banner"],
)

# ---------------------------------------------------------------------------
# Lookup for conditional sections keyed by service name
# ---------------------------------------------------------------------------

SERVICE_CONFIG_SECTIONS: dict[str, FormSection] = {
    "keycloak": KEYCLOAK_CONFIG_SECTION,
    "namespace-postgresql-database": POSTGRESQL_CONFIG_SECTION,
    "authorization-wall": AUTH_WALL_CONFIG_SECTION,
}

# ---------------------------------------------------------------------------
# All sections for easy iteration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Detail edit sections (for inline editing from project details page)
# ---------------------------------------------------------------------------

IDENTITY_EDIT_SECTION = FormSection(
    section_id="identity-edit",
    title="Projectgegevens bewerken",
    icon="document-blanco",
    description="Wijzig de weergavenaam en projectomschrijving",
    editables=[DISPLAY_NAME, DESCRIPTION],
    layout=["display-name", "description"],
    post_save_action="save_only",
)

COMPONENTS_EDIT_SECTION = FormSection(
    section_id="components-edit",
    title="Components beheren",
    icon="puzzel",
    description="Wijzig de applicatiecomponenten",
    enforcer=ComponentServicesEnforcer(),
    editables=COMPONENTS_SECTION.editables,
    layout=COMPONENTS_SECTION.layout,
    post_save_action="process_project",
)

SERVICES_EDIT_SECTION = FormSection(
    section_id="services-edit",
    title="Services beheren",
    icon="applicatie",
    description="Voeg services toe aan uw project",
    editables=[SERVICES],
    layout=["services"],
    post_save_action="process_project",
)

# Registry of sections available for detail-page editing.
# Includes edit-specific sections AND existing config sections.
EDIT_SECTIONS: dict[str, FormSection] = {
    "identity-edit": IDENTITY_EDIT_SECTION,
    "team-edit": TEAM_SECTION,
    "components-edit": COMPONENTS_EDIT_SECTION,
    "services-edit": SERVICES_EDIT_SECTION,
    "keycloak-config": KEYCLOAK_CONFIG_SECTION,
    "postgresql-config": POSTGRESQL_CONFIG_SECTION,
    "auth-wall-config": AUTH_WALL_CONFIG_SECTION,
}

# ---------------------------------------------------------------------------
# Dynamic section factories (parameterized by deployment index)
# ---------------------------------------------------------------------------


def build_domain_edit_section(deployment_index: int) -> FormSection:
    """Build a domain edit section targeting a specific deployment.

    Clones the wizard's DOMAIN_SECTION with all ``[0]`` path references
    replaced by ``[deployment_index]``, so the form reads/writes to the
    correct deployment in the YAML data.
    """
    from opi.forms.editables.reindex import reindex_layout, reindex_visualizer

    editables = [reindex_visualizer(e, 0, deployment_index) for e in DOMAIN_SECTION.editables]
    base_layout = DOMAIN_SECTION.layout if isinstance(DOMAIN_SECTION.layout, list) else []
    layout = reindex_layout(list(base_layout), 0, deployment_index)

    return FormSection(
        section_id=f"domain-edit-{deployment_index}",
        title="Webadres bewerken",
        icon="wereldbol",
        description="Wijzig het webadres voor deze deployment",
        enforcer=DomainConfigEnforcer(deployment_index=deployment_index),
        editables=editables,
        layout=layout,
        post_save_action="process_project",
    )


# ---------------------------------------------------------------------------
# All sections for easy iteration
# ---------------------------------------------------------------------------


def _prefix_layout_children(items: list, prefix: str) -> list:
    """Prefix relative paths in a layout with an absolute path prefix.

    Turns ``"name"`` into ``"components[0]/name"`` when *prefix* is
    ``"components[0]"``.  Recurses into Fieldset and Sequence children.
    """
    result: list = []
    for item in items:
        if isinstance(item, str):
            result.append(f"{prefix}/{item}")
        elif isinstance(item, Fieldset):
            replaced = dataclasses.replace(item, children=_prefix_layout_children(list(item.children), prefix))
            if replaced.depends_on:
                replaced = dataclasses.replace(replaced, depends_on=f"{prefix}/{replaced.depends_on}")
            result.append(replaced)
        elif isinstance(item, Sequence):
            child_layout = item.child_layout
            if isinstance(child_layout, list):
                child_layout = _prefix_layout_children(child_layout, prefix)
            result.append(
                dataclasses.replace(
                    item,
                    field_name=f"{prefix}/{item.field_name}" if item.field_name else item.field_name,
                    child_layout=child_layout,
                )
            )
        else:
            result.append(item)
    return result


def build_component_edit_section(component_index: int, is_new: bool = False) -> FormSection:
    """Build a component edit section targeting a specific component.

    Extracts child editables from COMPONENTS_SEQUENCE, materialises
    ``[*]`` wildcards to ``[component_index]``, and prefixes layout
    paths so the form reads/writes to the correct component slot.

    When *is_new* is True, the name field is made editable despite
    the modal wizard running in edit_mode (same pattern as add-deployment).
    """
    from opi.forms.editables.reindex import materialize_wildcard_visualizer

    editables = [materialize_wildcard_visualizer(e, component_index) for e in (COMPONENTS_SEQUENCE.children or [])]

    # For new components, allow name editing (override readonly_on_edit)
    if is_new:
        for i, vis in enumerate(editables):
            if vis.editable.yaml_path.endswith("/name"):
                editables[i] = dataclasses.replace(vis, readonly_on_edit=False)
                break

    # Extract child_layout from the Sequence element in COMPONENTS_SECTION
    child_layout: list = []
    section_layout = COMPONENTS_SECTION.layout
    if isinstance(section_layout, list) and section_layout:
        first_element = section_layout[0]
        if isinstance(first_element, Sequence):
            raw = first_element.child_layout
            if isinstance(raw, list):
                child_layout = list(raw)

    prefix = f"components[{component_index}]"
    layout = _prefix_layout_children(child_layout, prefix)

    title = "Component toevoegen" if is_new else "Component bewerken"
    description = "Configureer het nieuwe component" if is_new else "Wijzig de instellingen van dit component"

    return FormSection(
        section_id=f"component-edit-{component_index}",
        title=title,
        icon="puzzel",
        description=description,
        enforcer=ComponentServicesEnforcer(),
        editables=editables,
        layout=layout,
        post_save_action="process_project",
    )


def build_component_deployment_select_section(component_index: int) -> FormSection:
    """Build a section for selecting which deployments receive the new component.

    Shows a checkbox group with all deployment names (all checked by default).
    The ``post_merge`` hook distributes the component reference to each
    selected deployment using ``ListDistributor``.
    """
    from opi.forms.editables.distributors import ListDistributor
    from opi.forms.editables.editable import Editable, WidgetType
    from opi.forms.visualizers.visualizer import EditableVisualizer

    target_deployments_editable = Editable(
        yaml_path="_target_deployments",
        transient=True,
        values_provider="DeploymentSelectOptionsProvider",
    )
    target_deployments_vis = EditableVisualizer(
        editable=target_deployments_editable,
        widget=WidgetType.CHECKBOX_GROUP,
        label="Deployments",
        description="Selecteer de deployments waaraan dit component wordt toegevoegd.",
    )

    distributor = ListDistributor(
        source_key="_component_refs",
        target_path="deployments",
        match_field="name",
        merge_field="components",
    )

    def post_merge(project_data: dict[str, Any], wizard_data: dict[str, Any]) -> None:
        """Build component references and distribute to selected deployments."""
        selected = wizard_data.get("_target_deployments", [])
        if not isinstance(selected, list) or not selected:
            return

        # Get the new component's data
        components = project_data.get("components", [])
        if component_index >= len(components):
            return
        component = components[component_index]
        comp_name = component.get("name")
        comp_image = component.get("image")
        if not comp_name:
            return

        ref: dict[str, Any] = {"reference": comp_name}
        if comp_image:
            ref["image"] = comp_image

        # Build the temp structure for the distributor
        wizard_data["_component_refs"] = [{"name": dep_name, "components": [ref.copy()]} for dep_name in selected]
        distributor(project_data, wizard_data)

    return FormSection(
        section_id="component-deployment-select",
        title="Deployments",
        icon="raket",
        description="Kies in welke deployments dit component wordt opgenomen.",
        editables=[target_deployments_vis],
        layout=["_target_deployments"],
        post_merge=post_merge,
    )


def build_deployment_edit_section(
    deployment_index: int,
    component_count: int | None = None,
) -> FormSection:
    """Build a deployment edit section for editing component images and env vars.

    Takes the DEPLOYMENT_COMPONENTS_SEQ visualizer and replaces the
    ``deployments[*]`` segment with the concrete deployment index, keeping
    ``components[*]`` as a sequence wildcard so items are iterable.

    Args:
        deployment_index: Which deployment to edit.
        component_count: Total number of project-level components.
            When set, limits the sequence to at most this many items
            (a deployment cannot have more components than defined).
    """
    from opi.forms.editables.reindex import replace_segment_visualizer

    old_seg = "deployments[*]"
    new_seg = f"deployments[{deployment_index}]"

    # Build a focused sequence with only the fields we want editable
    ref_vis = replace_segment_visualizer(DEPLOYMENT_COMP_REFERENCE, old_seg, new_seg)
    image_vis = replace_segment_visualizer(DEPLOYMENT_COMP_IMAGE, old_seg, new_seg)
    env_vis = replace_segment_visualizer(DEPLOYMENT_COMP_USER_ENV_VARS, old_seg, new_seg)

    seq_vis = replace_segment_visualizer(DEPLOYMENT_COMPONENTS_SEQ, old_seg, new_seg)

    # Set max_items to the number of project components
    if component_count is not None:
        seq_ed = dataclasses.replace(seq_vis.editable, max_items=component_count)
        seq_vis = dataclasses.replace(seq_vis, editable=seq_ed)

    seq_vis = dataclasses.replace(seq_vis, children=[ref_vis, image_vis, env_vis])

    return FormSection(
        section_id=f"deployment-edit-{deployment_index}",
        title="Deployment bewerken",
        icon="server",
        description="Wijzig container images en omgevingsvariabelen per component",
        enforcer=UniqueReferencesEnforcer(),
        editables=[seq_vis],
        layout=[
            Sequence(
                field_name=f"deployments[{deployment_index}]/components",
                child_layout=[
                    "reference",
                    "image",
                    Fieldset(
                        legend="Omgevingsvariabelen",
                        description="Deployment-specifieke omgevingsvariabelen voor dit component.",
                        children=["user-env-vars"],
                    ),
                ],
            ),
        ],
        post_save_action="process_project",
    )


# ---------------------------------------------------------------------------
# Backup & Restore sections (modal wizard, no editables — custom templates)
# ---------------------------------------------------------------------------


def _backup_summary(data: dict[str, Any]) -> str:
    """Build review summary for backup wizard."""
    dep = data.get("deployment_name", "-")
    types = data.get("resource_types", [])
    if isinstance(types, str):
        types = [types]
    types_str = ", ".join(t.upper() for t in types) if types else "alle"
    return f"<p><strong>Deployment:</strong> {dep}</p><p><strong>Resource types:</strong> {types_str}</p>"


def _restore_select_summary(data: dict[str, Any]) -> str:
    """Build review summary for backup run selection."""
    run_id = data.get("backup_run_id", "-")
    return f"<p><strong>Backup run:</strong> <code>{run_id}</code></p>"


def _restore_target_summary(data: dict[str, Any]) -> str:
    """Build review summary for restore target selection."""
    from opi.services import RestoreMode

    restore_mode = data.get("restore_mode", RestoreMode.EXISTING.value)
    if restore_mode == RestoreMode.NEW.value:
        return "<p><strong>Modus:</strong> Nieuwe deployment</p>"
    target = data.get("target_deployment", "-")
    return f"<p><strong>Doel deployment:</strong> {target}</p>"


BACKUP_SELECT_SECTION = FormSection(
    section_id="backup-select",
    title="Backup configuratie",
    icon="database",
    description="Selecteer een deployment en welke resources u wilt back-uppen",
    editables=[],
    layout=[TemplatePartial(template="wizard/partials/backup_select_deployment.html.j2")],
    post_save_action="trigger_backup",
    summary_fn=_backup_summary,
)

RESTORE_SELECT_SECTION = FormSection(
    section_id="restore-select",
    title="Backup selecteren",
    icon="database",
    description="Selecteer een backup run om te herstellen",
    editables=[],
    layout=[TemplatePartial(template="wizard/partials/restore_select_backup.html.j2")],
    summary_fn=_restore_select_summary,
)

RESTORE_TARGET_SECTION = FormSection(
    section_id="restore-target",
    title="Doel selecteren",
    icon="applicatie",
    description="Kies waar de backup naartoe hersteld moet worden",
    editables=[],
    layout=[TemplatePartial(template="wizard/partials/restore_select_target.html.j2")],
    post_save_action="trigger_restore",
    summary_fn=_restore_target_summary,
)


def _new_deployment_summary(data: dict[str, Any], deployment_index: int = 0) -> str:
    """Build review summary for new deployment configuration."""
    deployments = data.get("deployments", [])
    name = deployments[deployment_index].get("name", "-") if deployment_index < len(deployments) else "-"
    return f"<p><strong>Deployment:</strong> {name}</p>"


def _materialize_new_deployment_fields(
    deployment_index: int,
    visualizers: list,
) -> list:
    """Materialize deployment[*] visualizers to a concrete deployment index.

    Shared helper used by both the restore-new-deployment section and the
    add-deployment wizard.
    """
    from opi.forms.editables.reindex import materialize_wildcard_visualizer

    return [materialize_wildcard_visualizer(vis, deployment_index) for vis in visualizers]


# The base set of visualizers for creating a new deployment (info + domain).
_NEW_DEPLOYMENT_INFO_VISUALIZERS = [DEPLOYMENT_NAME, DEPLOYMENT_CLONE_FROM]
_NEW_DEPLOYMENT_DOMAIN_VISUALIZERS = [
    DEPLOYMENT_SUBDOMAIN,
    DEPLOYMENT_BASE_DOMAIN,
    DEPLOYMENT_CUSTOM_BASE_DOMAIN,
    DEPLOYMENT_DOMAIN_FORMAT,
]


def _build_restore_new_deployment_section() -> FormSection:
    """Build the restore new deployment section with materialized editables.

    Materializes deployment[*] visualizers to deployment[0] so the form
    reads/writes a single deployment entry.
    """
    editables = _materialize_new_deployment_fields(
        0, _NEW_DEPLOYMENT_INFO_VISUALIZERS + _NEW_DEPLOYMENT_DOMAIN_VISUALIZERS
    )

    return FormSection(
        section_id="restore-new-deployment",
        title="Deployment configuratie",
        icon="server",
        description="Configureer de nieuwe deployment",
        visible=lambda data: data.get("restore_mode") == "new",
        editables=editables,
        layout=[
            "deployments[0]/name",
            "deployments[0]/clone-from",
            "deployments[0]/subdomain",
            "deployments[0]/base-domain",
            "deployments[0]/base-domain:custom",
            "deployments[0]/domain-format",
        ],
        summary_fn=_new_deployment_summary,
    )


RESTORE_NEW_DEPLOYMENT_SECTION = _build_restore_new_deployment_section()


# ---------------------------------------------------------------------------
# Add deployment sections (multi-step wizard from detail page)
# ---------------------------------------------------------------------------


def build_deployment_add_info_section(deployment_index: int) -> FormSection:
    """Build the info section for adding a new deployment (name + clone-from).

    Overrides readonly_on_edit on the name visualizer so the name field
    is editable even though the modal wizard uses edit_mode=True.
    """
    editables = _materialize_new_deployment_fields(deployment_index, _NEW_DEPLOYMENT_INFO_VISUALIZERS)

    # The name visualizer has readonly_on_edit=True — override for add flows
    name_vis = editables[0]
    editables[0] = dataclasses.replace(name_vis, readonly_on_edit=False)

    return FormSection(
        section_id=f"add-deployment-info-{deployment_index}",
        title="Deployment informatie",
        icon="server",
        description="Kies een naam en optioneel een bron-deployment om van te klonen",
        enforcer=UniqueDeploymentNameEnforcer(deployment_index=deployment_index),
        editables=editables,
        layout=[
            f"deployments[{deployment_index}]/name",
            f"deployments[{deployment_index}]/clone-from",
        ],
        summary_fn=lambda data, idx=deployment_index: _new_deployment_summary(data, idx),
    )


def build_deployment_add_components_section(
    deployment_index: int,
    component_count: int | None = None,
) -> FormSection:
    """Build the components section for adding a new deployment.

    Reuses the same logic as build_deployment_edit_section.
    """
    section = build_deployment_edit_section(deployment_index, component_count=component_count)
    return dataclasses.replace(
        section,
        section_id=f"add-deployment-components-{deployment_index}",
        title="Components",
        description="Selecteer de components en container images voor deze deployment",
    )


def build_deployment_add_domain_section(deployment_index: int) -> FormSection:
    """Build the domain section for adding a new deployment.

    Reuses build_domain_edit_section which already has the full DOMAIN_SECTION
    with URL preview, info partial, root-component, and DomainConfigEnforcer.
    """
    return build_domain_edit_section(deployment_index)


ALL_SECTIONS: list[FormSection] = [
    IDENTITY_SECTION,
    SERVICES_SECTION,
    KEYCLOAK_CONFIG_SECTION,
    POSTGRESQL_CONFIG_SECTION,
    AUTH_WALL_CONFIG_SECTION,
    TEAM_SECTION,
    COMPONENTS_SECTION,
    DOMAIN_SECTION,
    WIZARD_DEPLOYMENT_SECTION,
    DEPLOYMENTS_SECTION,
    CONFIG_DISPLAY_SECTION,
]
