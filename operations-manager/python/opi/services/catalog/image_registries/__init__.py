"""The ``image-registries`` service: where an image comes from, and with which secret.

The cluster table and the project's registries together form one rule list
(``resolution.py``). Only the provisioning has activation, and that is data driven: no
registries in the config means no manifest at all.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.core.config import settings
from opi.forms.layout import COMPONENT_IMAGE_SLOT, Div, Fieldset
from opi.forms.visualizers.sections import FormSection
from opi.services.catalog.base import (
    ConfigLayer,
    ConfigRole,
    DetailPageSection,
    ProjectManifestContext,
    ProjectManifestSpec,
    ProjectPageContext,
    Service,
    config_path,
)
from opi.services.catalog.events import on
from opi.services.catalog.image_registries.backends import (
    QuayProxyOrganizationBackend,
    backend_for_cluster,
    cluster_default_quota_gib,
)
from opi.services.catalog.image_registries.config_model import ComponentRegistryConfig, ImageRegistriesConfig
from opi.services.catalog.image_registries.editables import (
    COMPONENT_REGISTRY_EDITABLE,
    DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE,
    REGISTRIES_SEQUENCE_EDITABLE,
)
from opi.services.catalog.image_registries.enforcers import RegistryTokenEnforcer
from opi.services.catalog.image_registries.naming import registry_slug
from opi.services.catalog.image_registries.ownership import (
    validate_proxy_organization_claims,
    validate_proxy_organization_ownership,
    validate_registry_entry_ownership,
)
from opi.services.catalog.image_registries.quota import QUOTA_APPROVAL, effective_quota_gib, pending_quota_gib
from opi.services.catalog.image_registries.references import validate_registry_references
from opi.services.catalog.image_registries.resolution import project_registries
from opi.services.catalog.image_registries.visualizers import (
    COMPONENT_REGISTRY,
    DEPLOYMENT_COMPONENT_REGISTRY,
    REGISTRIES_SEQUENCE,
)
from opi.services.services import ServiceDefinition, service_entry_name
from opi.services.services_enums import ServiceType, UIEvent

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opi.forms.editables.editable import Editable
    from opi.forms.layout import LayoutChild
    from opi.forms.visualizers.visualizer import EditableVisualizer
    from opi.services.catalog.approval import ApprovalSpec

logger = logging.getLogger(__name__)


#: Shown up front where the cluster builds a proxy per registry: on staging the first pull
#: took three and a half minutes to work.
_PROXY_SETUP_NOTICE = (
    "Op dit platform maakt ODCN voor elke registry een eigen proxy aan. Dat duurt na het "
    "opslaan een paar minuten, en zolang kan een component zijn image nog niet ophalen. "
    "Dat lost zich vanzelf op."
)


def _section_description(cluster: str) -> str:
    """The step's description, with the proxy notice on a cluster that builds one."""
    description = "De private registries waaruit dit project images haalt."
    if isinstance(backend_for_cluster(cluster), QuayProxyOrganizationBackend):
        return f"{description} {_PROXY_SETUP_NOTICE}"
    return description


class ImageRegistriesService(Service):
    service_type = ServiceType.IMAGE_REGISTRIES
    definition = ServiceDefinition(
        name="Eigen container registries",
        description="Draai images uit je eigen private registry, met je eigen token",
        help_template="image_registries/help.md",
        icon="server",
        color="lichtblauw",
        variables=[],
    )
    config_model = ImageRegistriesConfig
    config_schema_version = "1.0"
    config_section_id = "image-registries-config"
    modal_flow_id = "modal-edit-image-registries-config"
    # The registries live at project level, so a component that picks a registry may enrol
    # itself there.
    allows_implicit_project_selection = True
    config_component_order = 8
    # The chosen registry IS the selection; see features/image-registries.md.
    component_selection_follows_config = True

    def config_model_for(self, layer: ConfigLayer) -> type[BaseModel] | None:
        # The registries on the project, the reference by name on a (deployment) component.
        if layer in (ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT_COMPONENT):
            return ComponentRegistryConfig
        return self.config_model

    def config_roles(self, layer: ConfigLayer) -> tuple[ConfigRole, ...]:
        # The project uses its own registries; a component binds one to its image.
        if layer is ConfigLayer.PROJECT:
            return (ConfigRole.USE,)
        return (ConfigRole.USE, ConfigRole.BIND)

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        if layer is ConfigLayer.PROJECT:
            return self.config_model_field_names()
        if layer in (ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT_COMPONENT):
            return ["registry"]
        return []

    def config_editables(self, layer: ConfigLayer) -> list[Editable]:
        if layer is ConfigLayer.PROJECT:
            return [REGISTRIES_SEQUENCE_EDITABLE]
        if layer is ConfigLayer.COMPONENT:
            return [COMPONENT_REGISTRY_EDITABLE]
        if layer is ConfigLayer.DEPLOYMENT_COMPONENT:
            return [DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE]
        return []

    def config_approvals(self, layer: ConfigLayer) -> list[ApprovalSpec]:
        return [QUOTA_APPROVAL.spec] if layer is ConfigLayer.PROJECT else []

    def ensure_approval_requests(self, project_data: dict[str, Any]) -> None:
        QUOTA_APPROVAL.ensure_requested(project_data)

    def config_component_visualizers(self) -> list[EditableVisualizer]:
        return [COMPONENT_REGISTRY]

    def config_component_layout(self) -> list[LayoutChild]:
        svc = self.service_type.value
        # No ``depends_on`` on the services list: the choice IS the selection, so the field
        # would wait on what it sets itself.
        return [
            Div(
                slot=COMPONENT_IMAGE_SLOT,
                children=[f"services{{{svc}}}/config/registry"],
            )
        ]

    def config_deployment_component_visualizers(self) -> list[EditableVisualizer]:
        return [DEPLOYMENT_COMPONENT_REGISTRY]

    def config_deployment_component_layout(self) -> list[LayoutChild]:
        svc = self.service_type.value
        # ``services`` is a dict keyed on service name here, so a plain path segment. The
        # fieldset is unconditional, because the component's services list is not a field of
        # this form.
        return [
            Fieldset(
                legend="Eigen registry (alleen voor deze deployment)",
                description=(
                    "Standaard volgt deze deployment de registry van het component. "
                    "Vul dit alleen in als deze deployment een andere registry moet gebruiken."
                ),
                children=[f"services/{svc}/config/registry"],
            )
        ]

    def generate_missing_values(self, project_data: dict[str, Any]) -> dict[str, str]:
        """Give every registry without a ``name`` one, derived from its label.

        Here and not in a converter of the editable, because both write paths come past here:
        the portal through the section's ``post_merge`` and the API through
        ``registry.generate_missing_values``. An existing name stays, also when the label
        changes.
        """
        # Lazy: ``opi.services.project`` reads ``opi.forms``, which reads this module through
        # the providers.
        from opi.services.project import Project

        base = config_path(ConfigLayer.PROJECT, self.service_type, "config", "registries")
        registries = Project(project_data).get(base) or []
        if not isinstance(registries, list):
            return {}
        used = {entry["name"] for entry in registries if isinstance(entry, dict) and entry.get("name")}
        generated: dict[str, str] = {}
        for index, entry in enumerate(registries):
            if not isinstance(entry, dict) or entry.get("name") or not entry.get("display-name"):
                continue
            slug = registry_slug(str(entry["display-name"]), used)
            entry["name"] = slug
            used.add(slug)
            generated[f"{base}[{index}]/name"] = slug
        if generated:
            logger.info(
                f"Registrynaam afgeleid voor {len(generated)} registry(s) van project "
                f"'{project_data.get('name', 'unknown')}'"
            )
        return generated

    def _generate_missing_names(self, project_data: dict[str, Any], _form_data: dict[str, Any]) -> None:
        """The ``post_merge`` shape of :meth:`generate_missing_values`: the portal hands two
        dicts and wants nothing back, and one implementation serves both paths."""
        self.generate_missing_values(project_data)

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    def config_form_section(self, layer: ConfigLayer) -> FormSection | None:
        if layer is not ConfigLayer.PROJECT:
            return super().config_form_section(layer)
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            cached = FormSection(
                section_id=self.config_section_id or "image-registries-config",
                title="Eigen container registries",
                icon="server",
                description=_section_description(settings.CLUSTER_MANAGER),
                visible=self._config_selected,
                post_save_action="process_project",
                editables=[REGISTRIES_SEQUENCE],
                layout=[config_path(ConfigLayer.PROJECT, self.service_type, "config", "registries")],
                enforcer=RegistryTokenEnforcer(),
                post_merge=self._generate_missing_names,
            )
            self._config_section_cache = cached
        return cached

    @on(UIEvent.PROJECT_SECTIONS)
    def registries_block(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
        """What this project has in private registries.

        The proxy's state lives in the cluster and is fetched lazily (``web.py``).
        """
        registries = project_registries(ctx.project_data)
        if not registries:
            return []
        # Only what the template shows. ``ctx.project_data`` is DECRYPTED, so the entry itself
        # carries the token in plain text; that does not belong in a render context. Of the
        # token only the QUESTION whether it is there travels along: with the username
        # optional, that is the only way the block recognises an entry with credentials.
        # A quota belongs to the Organization, which exists only on a proxy cluster and not
        # for a row that points at an existing secret (``backends.py``).
        on_proxy = isinstance(backend_for_cluster(settings.CLUSTER_MANAGER), QuayProxyOrganizationBackend)
        cluster_default = cluster_default_quota_gib(settings.CLUSTER_MANAGER)
        shown = []
        for registry in registries:
            name = str(registry.get("name", ""))
            entry: dict[str, Any] = {
                **{key: registry.get(key) for key in ("name", "upstream", "username", "secretName")},
                "has_token": bool(registry.get("password")),
            }
            if on_proxy and not registry.get("secretName"):
                entry["effective_quota_gib"] = effective_quota_gib(ctx.project_data, name, cluster_default)
                entry["requested_quota_gib"] = pending_quota_gib(ctx.project_data, name)
            shown.append(entry)
        return [
            DetailPageSection(
                template="image_registries/section-detail.html.j2",
                context={"registries": shown, "project_name": ctx.project_data.get("name", "")},
            )
        ]

    def web_routers(self) -> list[Any]:
        # Lazy: the router reads the project store, which reads this module through
        # ``project_validation`` and ``opi.forms``.
        from opi.services.catalog.image_registries.web import image_registries_router

        return [*super().web_routers(), image_registries_router]

    def validate_project(self, project_data: dict[str, Any]) -> list[str]:
        """The three ownership rules around the proxy organizations (see ``ownership.py``),
        plus the way back (``references.py``).

        The first three look at the other projects on the cluster and not at a config block,
        so they cannot live in ``validate_config``. They also run when this project does not
        tick the service: what matters is what an image points AT. The fourth lays components
        next to the registry list and belongs here for the same reason.
        """
        return [
            *validate_proxy_organization_ownership(project_data),
            *validate_registry_entry_ownership(project_data),
            *validate_proxy_organization_claims(project_data),
            *validate_registry_references(project_data),
        ]

    def contribute_project_manifests(self, ctx: ProjectManifestContext) -> list[ProjectManifestSpec]:
        """What this service puts at the PROJECT level of the deployments repo."""
        registries = project_registries(ctx.project_data)
        if not registries:
            return []
        backend = backend_for_cluster(ctx.cluster)
        specs: list[ProjectManifestSpec] = []
        seen: set[str] = set()
        for registry in registries:
            for spec in backend.manifests(ctx, registry):
                if spec.filename in seen:
                    # Two registries on the same filename: the first wins, as in the rule
                    # list.
                    logger.warning(
                        f"Registry '{registry.get('name')}' van project '{ctx.project_name}' levert dezelfde "
                        f"projectmanifest '{spec.filename}' als een eerdere registry; de eerste blijft staan"
                    )
                    continue
                seen.add(spec.filename)
                specs.append(spec)
        return specs
