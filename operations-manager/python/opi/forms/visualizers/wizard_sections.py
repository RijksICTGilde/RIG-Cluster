"""Wizard section definitions for project forms.

Each FormSection groups related editables into a logical wizard step.
Step ordering is determined by the sections list in FormFlow, NOT by
any property on the section itself.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from opi.forms.editables.editable import Editable, WidgetType
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
    BACKUP_DEPLOYMENT_NAME,
    BACKUP_RESOURCE_TYPES,
    DEPLOYMENT_BACKUP_RESOURCE_TYPES,
    DEPLOYMENT_BACKUP_SCHEDULE,
    DEPLOYMENT_BACKUP_SCHEDULE_DAY,
    DEPLOYMENT_BACKUP_SCHEDULE_MONTHDAY,
    DEPLOYMENT_BACKUP_SCHEDULE_TIME,
    DEPLOYMENT_CERT_COMPONENTS_SEQ,
    DEPLOYMENT_CLONE_FROM,
    DEPLOYMENT_COMP_ATTACHMENT_USE_SEQUENCE,
    DEPLOYMENT_COMP_IMAGE,
    DEPLOYMENT_COMP_REFERENCE,
    DEPLOYMENT_COMPONENTS_SEQ,
    DEPLOYMENT_NAME,
    DEPLOYMENTS_SEQUENCE,
)
from opi.forms.visualizers.fields.domains import (
    DOMAIN_CONFIG,
    WIZARD_DEPLOYMENT_NAME,
)
from opi.forms.visualizers.fields.identity import CLUSTERS, DESCRIPTION, DISPLAY_NAME
from opi.forms.visualizers.fields.services import (
    SERVICES,
)
from opi.forms.visualizers.fields.team import USERS_SEQUENCE
from opi.forms.visualizers.sections import FormSection
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.base import ConfigLayer
from opi.services.registry import deployment_component_service_visualizers, get_service
from opi.services.services import ServiceAdapter, service_entry_name
from opi.services.services_enums import ServiceType


def _extract_services(data: dict[str, Any]) -> list[str]:
    """Extract active service names from wizard form data."""
    services = data.get("services", [])
    if isinstance(services, str):
        return [services] if services else []
    if isinstance(services, list):
        return extract_service_names(services)
    return []


def _service_component_layouts() -> list[Any]:
    """Collect the per-component layout nodes each service hooks into the component
    form (RC-5 'service owns its fields'), in registry order. A component-level service
    (metrics-scraper, ...) owns its fieldset via ``config_component_layout()`` instead
    of it living hand-authored in COMPONENTS_SECTION."""
    contributors = sorted(
        (get_service(service_type) for service_type in ServiceType),
        key=lambda s: s.config_component_order,
    )
    nodes: list[Any] = []
    for service in contributors:
        nodes.extend(service.config_component_layout())
    return nodes


def _service_deployment_component_layouts() -> list[Any]:
    """Collect the per-deployment-component layout nodes each service hooks into the
    deployment-edit form (RC-25), in ``config_component_order``.

    The deployment-component counterpart of ``_service_component_layouts``. Before RC-25
    this layer had no service-owned hook, so its one fieldset was hand-authored here.
    """
    contributors = sorted(
        (get_service(service_type) for service_type in ServiceType),
        key=lambda s: s.config_component_order,
    )
    nodes: list[Any] = []
    for service in contributors:
        nodes.extend(service.config_deployment_component_layout())
    return nodes


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
                        "resources/requests/memory",
                        "resources/limits/memory",
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
                        Sequence(field_name="path"),
                    ],
                ),
                # Component-level services hook their fieldsets/sequences in here,
                # collected from the service catalog in config_component_order. The
                # aliases + user-env-vars system services sort first (RC-25), so they
                # land exactly where the hand-authored "Variabelen" fieldset used to sit.
                *_service_component_layouts(),
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

# RC-5: the keycloak service owns its config section (built by
# KeycloakService.config_form_section); re-exported here so flows / EDIT_SECTIONS /
# tests keep referring to it. The nested additional-clients editables stay in the
# forms layer; the service references them.


def _with_service_help(section: FormSection | None, service_type: ServiceType) -> FormSection | None:
    """Stamp the service's own explanation onto its config section.

    The question mark that opens it already exists on the service card and in the
    overview; the config screen is where someone actually has to decide something, and
    it was the one place without it. Done here, where the sections are collected, so a
    service declares its help once on its ServiceDefinition and every surface picks it up.
    """
    if section is not None and section.help_template is None:
        section.help_template = ServiceAdapter.get_service_definition(service_type).help_template
    return section


KEYCLOAK_CONFIG_SECTION = _with_service_help(
    get_service(ServiceType.KEYCLOAK).config_form_section(ConfigLayer.PROJECT), ServiceType.KEYCLOAK
)

# RC-5: the namespace-postgres service owns its config section (built by
# NamespacePostgresqlDatabaseService.config_form_section); re-exported here under the
# familiar name so flows / EDIT_SECTIONS / tests keep referring to it.
POSTGRESQL_CONFIG_SECTION = _with_service_help(
    get_service(ServiceType.NAMESPACE_POSTGRESQL_DATABASE).config_form_section(ConfigLayer.PROJECT),
    ServiceType.NAMESPACE_POSTGRESQL_DATABASE,
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
        "deployments[*]/base-domain",
        "deployments[*]/_request-domain",
        "deployments[*]/base-domain:custom",
        "deployments[*]/domain-format",
        "deployments[*]/subdomain",
        "deployments[*]/_request-subdomain",
        "deployments[*]/root-component",
        "deployments[*]/expose-component-on-bare-domain",
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
        "deployments[*]/name",
    ],
)


def build_deployment_wizard_section(deployment_index: int) -> FormSection:
    """Build a deployment wizard section targeting a specific deployment.

    Materializes the WIZARD_DEPLOYMENT_SECTION wildcards (``[*]``) to
    ``[deployment_index]``.
    """
    from opi.forms.editables.reindex import materialize_wildcard_layout, materialize_wildcard_visualizer

    editables = [materialize_wildcard_visualizer(e, deployment_index) for e in WIZARD_DEPLOYMENT_SECTION.editables]
    base_layout = WIZARD_DEPLOYMENT_SECTION.layout if isinstance(WIZARD_DEPLOYMENT_SECTION.layout, list) else []
    layout = materialize_wildcard_layout(list(base_layout), deployment_index)

    return FormSection(
        section_id=WIZARD_DEPLOYMENT_SECTION.section_id,
        title=WIZARD_DEPLOYMENT_SECTION.title,
        icon=WIZARD_DEPLOYMENT_SECTION.icon,
        description=WIZARD_DEPLOYMENT_SECTION.description,
        editables=editables,
        layout=layout,
    )


# RC-5 prototype: the authorization-wall service now OWNS its config section - it is
# built by AuthorizationWallProvider.config_form_section() and merely re-exported here
# under the familiar name, so flows.py / EDIT_SECTIONS / tests keep referring to it.
# (keycloak / postgres sections still live hand-authored above until they follow.)
AUTH_WALL_CONFIG_SECTION = _with_service_help(
    get_service(ServiceType.AUTHORIZATION_WALL).config_form_section(ConfigLayer.PROJECT), ServiceType.AUTHORIZATION_WALL
)

# sleep-mode owns its project-level config section (SleepModeService.config_form_section),
# re-exported here so the derived SERVICE_CONFIG_SECTIONS picks it up by config_section_id.
SLEEP_MODE_CONFIG_SECTION = _with_service_help(
    get_service(ServiceType.SLEEP_MODE).config_form_section(ConfigLayer.PROJECT), ServiceType.SLEEP_MODE
)

# postgresql-database owns its project-level schema-list section
# (PostgresqlDatabaseService.config_form_section), re-exported so the derived
# SERVICE_CONFIG_SECTIONS picks it up by config_section_id.
POSTGRESQL_SCHEMAS_SECTION = _with_service_help(
    get_service(ServiceType.POSTGRESQL_DATABASE).config_form_section(ConfigLayer.PROJECT),
    ServiceType.POSTGRESQL_DATABASE,
)

# invite owns its project-level config section (InviteService.config_form_section),
# re-exported here so the derived SERVICE_CONFIG_SECTIONS picks it up by config_section_id.
INVITE_CONFIG_SECTION = _with_service_help(
    get_service(ServiceType.INVITE).config_form_section(ConfigLayer.PROJECT), ServiceType.INVITE
)

# cross-domain-access owns its project-level config section
# (CrossDomainAccessService.config_form_section), re-exported here so the derived
# SERVICE_CONFIG_SECTIONS picks it up by config_section_id.
CROSS_DOMAIN_CONFIG_SECTION = _with_service_help(
    get_service(ServiceType.CROSS_DOMAIN_ACCESS).config_form_section(ConfigLayer.PROJECT),
    ServiceType.CROSS_DOMAIN_ACCESS,
)

# redis and minio-storage own project-level settings that had a model and an API route but
# no form field anywhere (RC-25); their sections are built by the services and re-exported
# here so the derived SERVICE_CONFIG_SECTIONS picks them up by config_section_id.
REDIS_CONFIG_SECTION = _with_service_help(
    get_service(ServiceType.REDIS).config_form_section(ConfigLayer.PROJECT), ServiceType.REDIS
)
MINIO_CONFIG_SECTION = _with_service_help(
    get_service(ServiceType.MINIO_STORAGE).config_form_section(ConfigLayer.PROJECT), ServiceType.MINIO_STORAGE
)

# ---------------------------------------------------------------------------
# Lookup for conditional sections keyed by service name
# ---------------------------------------------------------------------------

# Per-service config sections indexed by section_id, so the four service dicts can be
# DERIVED from the provider registry (config_section_id) instead of hand-synced
# (RC-5 Phase 3). Adding a service's config section = define it here + declare
# config_section_id on its provider.
_CONFIG_SECTIONS_BY_ID: dict[str, FormSection] = {
    section.section_id: section
    for section in (
        KEYCLOAK_CONFIG_SECTION,
        POSTGRESQL_CONFIG_SECTION,
        POSTGRESQL_SCHEMAS_SECTION,
        AUTH_WALL_CONFIG_SECTION,
        SLEEP_MODE_CONFIG_SECTION,
        INVITE_CONFIG_SECTION,
        CROSS_DOMAIN_CONFIG_SECTION,
        REDIS_CONFIG_SECTION,
        MINIO_CONFIG_SECTION,
    )
}

# service name -> config FormSection, derived by iterating the provider registry.
SERVICE_CONFIG_SECTIONS: dict[str, FormSection] = {
    service_type.value: _CONFIG_SECTIONS_BY_ID[provider.config_section_id]
    for service_type in ServiceType
    if (provider := get_service(service_type)).config_section_id in _CONFIG_SECTIONS_BY_ID
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


def _strip_removed_services_from_components(
    project_data: dict[str, Any],
    _wizard_data: dict[str, Any],
) -> None:
    """Remove services from components that are no longer in the project services list.

    When a user removes a service from the root ``services`` list, the
    component-level ``services`` lists must be updated to match.  Otherwise
    the YAML becomes inconsistent and the service-removal detection in
    ``cleanup_removed_services_from_yaml_change`` won't fire (it checks
    component-level usage).
    """
    project_services = set(_extract_services(project_data))
    for comp in project_data.get("components", []):
        if not isinstance(comp, dict):
            continue
        comp_services = comp.get("services")
        if not isinstance(comp_services, list):
            continue
        # Use the canonical helper: the previous local reader ignored the component
        # ``{reference: X, config: Y}`` two-key record and returned None for it, so a
        # storage/config-carrying entry was stripped out as "not a project service".
        comp["services"] = [svc for svc in comp_services if service_entry_name(svc) in project_services]


# Wire the same component-reconciliation hook onto the create-wizard services
# step. Defined here (not in the constructor at the top of the file) because
# the helper is declared below SERVICES_SECTION. Without this, deselecting a
# project service in the create wizard left orphaned component-level service
# config until the components step was re-submitted (one navigation late).
SERVICES_SECTION.post_merge = _strip_removed_services_from_components


SERVICES_EDIT_SECTION = FormSection(
    section_id="services-edit",
    title="Services beheren",
    icon="applicatie",
    description="Voeg services toe aan uw project",
    editables=[SERVICES],
    layout=["services"],
    post_save_action="process_project",
    post_merge=_strip_removed_services_from_components,
)

# Registry of sections available for detail-page editing.
# Includes edit-specific sections AND existing config sections.
EDIT_SECTIONS: dict[str, FormSection] = {
    "identity-edit": IDENTITY_EDIT_SECTION,
    "team-edit": TEAM_SECTION,
    "components-edit": COMPONENTS_EDIT_SECTION,
    "services-edit": SERVICES_EDIT_SECTION,
    # Per-service config sections, derived from the registry (keyed by section_id).
    **{section.section_id: section for section in SERVICE_CONFIG_SECTIONS.values()},
}

# ---------------------------------------------------------------------------
# Dynamic section factories (parameterized by deployment index)
# ---------------------------------------------------------------------------


def build_domain_section(deployment_index: int, *, edit_mode: bool = False) -> FormSection:
    """Build a domain section targeting a specific deployment.

    Materializes the DOMAIN_SECTION wildcards (``[*]``) to
    ``[deployment_index]``, so the form reads/writes to the correct
    deployment in the YAML data.

    In edit mode the section triggers a project reprocess on save;
    in create mode the wizard handles submission as a whole.
    """
    from opi.forms.editables.reindex import materialize_wildcard_layout, materialize_wildcard_visualizer

    editables = [materialize_wildcard_visualizer(e, deployment_index) for e in DOMAIN_SECTION.editables]
    base_layout = DOMAIN_SECTION.layout if isinstance(DOMAIN_SECTION.layout, list) else []
    layout = materialize_wildcard_layout(list(base_layout), deployment_index)

    if edit_mode:
        section_id = f"domain-edit-{deployment_index}"
        title = "Webadres bewerken"
        description = "Wijzig het webadres voor deze deployment"
        post_save_action = "process_project"
    else:
        section_id = "domains"
        title = DOMAIN_SECTION.title
        description = DOMAIN_SECTION.description
        post_save_action = "save_only"

    return FormSection(
        section_id=section_id,
        title=title,
        icon=DOMAIN_SECTION.icon,
        description=description,
        enforcer=DomainConfigEnforcer(deployment_index=deployment_index),
        editables=editables,
        layout=layout,
        post_save_action=post_save_action,
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
        comp_image = component.get(
            "image"
        )  # TODO: clarify purpose — see features/component-image-field-clarification.md
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
    # Deployment-component fields the services own (RC-25: user-env-vars), instead of
    # this function naming them one by one.
    service_vis = [
        replace_segment_visualizer(vis, old_seg, new_seg) for vis in deployment_component_service_visualizers()
    ]
    attach_vis = replace_segment_visualizer(DEPLOYMENT_COMP_ATTACHMENT_USE_SEQUENCE, old_seg, new_seg)

    seq_vis = replace_segment_visualizer(DEPLOYMENT_COMPONENTS_SEQ, old_seg, new_seg)

    # Set max_items to the number of project components
    if component_count is not None:
        seq_ed = dataclasses.replace(seq_vis.editable, max_items=component_count)
        seq_vis = dataclasses.replace(seq_vis, editable=seq_ed)

    seq_vis = dataclasses.replace(seq_vis, children=[ref_vis, image_vis, *service_vis, attach_vis])

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
                    *_service_deployment_component_layouts(),
                    Sequence(field_name="services/attachments/config"),
                ],
            ),
        ],
        post_save_action="process_project",
    )


def build_domain_cert_section(deployment_index: int) -> FormSection:
    """Per-component TLS-mode step for the domain wizard (modal-edit-domain).

    Lists the deployment's components (reference read-only, no add/remove) with a
    per-component TLS-mode override (erven/standard/passthrough/provided) and, for
    'provided', a certificate-attachment picker. Saves to
    deployments[N].components[*].publish-on-web.config.
    """
    from opi.forms.editables.reindex import replace_segment_visualizer

    old_seg = "deployments[*]"
    new_seg = f"deployments[{deployment_index}]"
    seq_vis = replace_segment_visualizer(DEPLOYMENT_CERT_COMPONENTS_SEQ, old_seg, new_seg)

    return FormSection(
        section_id=f"domain-cert-{deployment_index}",
        title="Certificaten per component",
        icon="sleutel",
        description="Kies per component hoe het TLS-certificaat geregeld wordt",
        editables=[seq_vis],
        layout=[
            Sequence(
                field_name=f"deployments[{deployment_index}]/components",
                child_layout=[
                    "reference",
                    Fieldset(
                        legend="Certificaat",
                        children=[
                            "services/publish-on-web/config/tls",
                            "services/publish-on-web/config/attachment",
                        ],
                    ),
                ],
            ),
        ],
        post_save_action="process_project",
    )


# ---------------------------------------------------------------------------
# Backup & Restore sections (modal wizard, no editables - custom templates)
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
    editables=[BACKUP_DEPLOYMENT_NAME, BACKUP_RESOURCE_TYPES],
    layout=["deployment_name", "resource_types"],
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

# RC-5: the attachments service owns its "Bijlagen" upload section (built by
# AttachmentsService.config_form_section, incl. the hidden read-only services carrier);
# re-exported here so flows / tests keep referring to it.
ATTACHMENTS_SECTION = _with_service_help(
    get_service(ServiceType.ATTACHMENTS).config_form_section(ConfigLayer.PROJECT), ServiceType.ATTACHMENTS
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


def _restore_new_visible(data: dict[str, Any]) -> bool:
    return data.get("restore_mode") == "new"


def build_restore_new_deployment_sections(deployment_index: int = 0) -> list[FormSection]:
    """Build sections for the restore-to-new-deployment flow.

    Reuses the add-deployment builders (info, components, domain) with
    clone-from excluded — the backup source is already selected in step 2.
    All sections are conditionally visible based on ``restore_mode == "new"``.

    Args:
        deployment_index: Index of the new deployment slot in the
            deployments list. Set at init time to ``len(deployments)``
            so it targets an empty slot, not an existing deployment.
    """
    info = build_deployment_add_info_section(deployment_index, include_clone_from=False)
    components = build_deployment_add_components_section(deployment_index)
    domain = build_deployment_add_domain_section(deployment_index)

    return [
        dataclasses.replace(info, visible=_restore_new_visible),
        dataclasses.replace(components, visible=_restore_new_visible),
        dataclasses.replace(domain, visible=_restore_new_visible),
    ]


# ---------------------------------------------------------------------------
# Add deployment sections (multi-step wizard from detail page)
# ---------------------------------------------------------------------------


def build_deployment_add_info_section(
    deployment_index: int,
    *,
    include_clone_from: bool = True,
) -> FormSection:
    """Build the info section for adding a new deployment.

    Overrides readonly_on_edit on the name visualizer so the name field
    is editable even though the modal wizard uses edit_mode=True.

    Args:
        include_clone_from: When False, omits the clone-from field (used
            by the restore flow where the source is already selected).
    """
    visualizers = _NEW_DEPLOYMENT_INFO_VISUALIZERS if include_clone_from else [DEPLOYMENT_NAME]
    editables = _materialize_new_deployment_fields(deployment_index, visualizers)

    # The name visualizer has readonly_on_edit=True - override for add flows
    name_vis = editables[0]
    editables[0] = dataclasses.replace(name_vis, readonly_on_edit=False)

    layout = [f"deployments[{deployment_index}]/name"]
    if include_clone_from:
        layout.append(f"deployments[{deployment_index}]/clone-from")

    return FormSection(
        section_id=f"add-deployment-info-{deployment_index}",
        title="Deployment informatie",
        icon="server",
        description="Kies een naam en optioneel een bron-deployment om van te klonen"
        if include_clone_from
        else "Kies een naam voor de nieuwe deployment",
        enforcer=UniqueDeploymentNameEnforcer(deployment_index=deployment_index),
        editables=editables,
        layout=layout,
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

    Reuses build_domain_section which already has the full DOMAIN_SECTION
    with URL preview, info partial, root-component, and DomainConfigEnforcer.
    """
    return build_domain_section(deployment_index, edit_mode=True)


# ---------------------------------------------------------------------------
# Admin domain/subdomain approval section
# ---------------------------------------------------------------------------


def _apply_approval_to_project(
    project_data: dict[str, Any],
    wizard_data: dict[str, Any],
) -> None:
    """Apply the submitted approval verdicts back onto the project (catalog-driven).

    Delegates to the generic approver interface, which routes each item to the service
    spec that owns it (``record``) instead of a hard-coded domain/subdomain switch.
    """
    from opi.services.approvals import apply_approval_verdicts

    items = wizard_data.get("_approval_items", [])
    if not items:
        return
    admin_email = wizard_data.get("_admin_email", "admin")
    apply_approval_verdicts(project_data, items, admin_email)


def build_domain_approval_section() -> FormSection:
    """Build the admin domain/subdomain approval section.

    Uses the same pattern as backup/restore: no editables, a TemplatePartial
    for the UI, and raw form data stored directly. The post_merge callback
    maps the submitted data back to the project YAML.
    """
    return FormSection(
        section_id="domain-approval",
        title="Domein- en subdomeingoedkeuring",
        icon="vinkje",
        description="Keur domein- en subdomeinaanvragen goed of af",
        editables=[],
        layout=[TemplatePartial(template="wizard/partials/approval_items.html.j2")],
        post_save_action="process_project",
        post_merge=_apply_approval_to_project,
    )


def build_backup_schedule_section(deployment_index: int) -> FormSection:
    """Build a backup schedule section targeting a specific deployment.

    Materializes deployment[*] to the concrete deployment index for the
    backup schedule SELECT field.
    """
    from opi.forms.editables.reindex import materialize_wildcard_visualizer

    # Transient fields must be processed BEFORE the main schedule field so that
    # RRuleFrequencyConverter.write() can read time/day/monthday from the result
    # dict when building the combined RRULE string.
    # The `layout` list below controls the display order independently.
    editables = [
        materialize_wildcard_visualizer(DEPLOYMENT_BACKUP_SCHEDULE_TIME, deployment_index),
        materialize_wildcard_visualizer(DEPLOYMENT_BACKUP_SCHEDULE_DAY, deployment_index),
        materialize_wildcard_visualizer(DEPLOYMENT_BACKUP_SCHEDULE_MONTHDAY, deployment_index),
        materialize_wildcard_visualizer(DEPLOYMENT_BACKUP_RESOURCE_TYPES, deployment_index),
        materialize_wildcard_visualizer(DEPLOYMENT_BACKUP_SCHEDULE, deployment_index),
    ]

    def _has_backupable_services(yaml_data: dict[str, Any], idx: int = deployment_index) -> bool:
        from opi.handlers.project_file_handler import create_project_file_handler

        deployments = yaml_data.get("deployments", [])
        if not isinstance(deployments, list) or idx >= len(deployments):
            return True
        dep = deployments[idx]
        dep_name = dep.get("name", "") if isinstance(dep, dict) else ""
        if not dep_name:
            return True
        pfh = create_project_file_handler()
        return bool(pfh.get_deployment_backup_labels(yaml_data, dep_name))

    async def _notify_backup_scheduler(request: Any) -> None:
        scheduler = getattr(request.app.state, "backup_scheduler", None)
        if scheduler:
            await scheduler.trigger_check()

    return FormSection(
        section_id=f"backup-schedule-{deployment_index}",
        title="Backup schema instellen",
        icon="database",
        description="Configureer automatische backups voor deze deployment. Tijden zijn een indicatie.",
        editables=editables,
        layout=[
            f"deployments[{deployment_index}]/backup/schedule",
            f"deployments[{deployment_index}]/backup/schedule:time",
            f"deployments[{deployment_index}]/backup/schedule:day",
            f"deployments[{deployment_index}]/backup/schedule:monthday",
            f"deployments[{deployment_index}]/backup/resource_types",
        ],
        guard=_has_backupable_services,
        guard_message="Deze deployment gebruikt geen services die geback-upt kunnen worden.",
        post_save_action="save_only",
        after_save=_notify_backup_scheduler,
    )


ALL_SECTIONS: list[FormSection] = [
    IDENTITY_SECTION,
    SERVICES_SECTION,
    KEYCLOAK_CONFIG_SECTION,
    POSTGRESQL_CONFIG_SECTION,
    POSTGRESQL_SCHEMAS_SECTION,
    AUTH_WALL_CONFIG_SECTION,
    TEAM_SECTION,
    COMPONENTS_SECTION,
    DOMAIN_SECTION,
    WIZARD_DEPLOYMENT_SECTION,
    DEPLOYMENTS_SECTION,
    CONFIG_DISPLAY_SECTION,
]
