"""De dienst ``image-registries``: welke image waar vandaan komt, en met welk secret.

De clustertabel en de registries van het project vormen samen één regellijst
(``resolution.py``). Alleen het provisioneren kent activatie, en dat is door data
gedreven: geen registries in de config betekent geen enkel manifest.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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
from opi.services.catalog.image_registries.backends import backend_for_cluster
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
from opi.services.catalog.image_registries.references import validate_registry_references
from opi.services.catalog.image_registries.resolution import project_registries
from opi.services.catalog.image_registries.visualizers import (
    COMPONENT_REGISTRY,
    DEPLOYMENT_COMPONENT_REGISTRY,
    REGISTRIES_SEQUENCE,
)
from opi.services.services import ServiceDefinition, service_entry_name
from opi.services.services_enums import ServiceBinding, ServiceType, UIEvent

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opi.forms.visualizers.visualizer import EditableVisualizer

logger = logging.getLogger(__name__)


class ImageRegistriesService(Service):
    service_type = ServiceType.IMAGE_REGISTRIES
    definition = ServiceDefinition(
        name="Eigen container registries",
        description="Draai images uit je eigen private registry, met je eigen token",
        help_template="image_registries/help.md",
        icon="server",
        color="lichtblauw",
        binding=ServiceBinding.COMPONENT,
        variables=[],
    )
    config_model = ImageRegistriesConfig
    config_schema_version = "1.0"
    config_section_id = "image-registries-config"
    modal_flow_id = "modal-edit-image-registries-config"
    # De registries staan op projectniveau, dus een component dat een registry kiest mag
    # zichzelf daar bijschrijven.
    allows_implicit_project_selection = True
    config_component_order = 8
    # De gekozen registry IS de selectie; zie features/image-registries.md.
    component_selection_follows_config = True

    def config_model_for(self, layer: ConfigLayer) -> type[BaseModel] | None:
        # De registries op het project, de verwijzing bij naam op (deployment-)component.
        if layer in (ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT_COMPONENT):
            return ComponentRegistryConfig
        return self.config_model

    def config_roles(self, layer: ConfigLayer):
        # Het project gebruikt zijn eigen registries; een component bindt er een aan zijn image.
        if layer is ConfigLayer.PROJECT:
            return (ConfigRole.USE,)
        return (ConfigRole.USE, ConfigRole.BIND)

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        if layer is ConfigLayer.PROJECT:
            return self.config_model_field_names()
        if layer in (ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT_COMPONENT):
            return ["registry"]
        return []

    def config_editables(self, layer: ConfigLayer):
        if layer is ConfigLayer.PROJECT:
            return [REGISTRIES_SEQUENCE_EDITABLE]
        if layer is ConfigLayer.COMPONENT:
            return [COMPONENT_REGISTRY_EDITABLE]
        if layer is ConfigLayer.DEPLOYMENT_COMPONENT:
            return [DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE]
        return []

    # --- component- en deployment-componentniveau -------------------------------------

    def config_component_visualizers(self) -> list[EditableVisualizer]:
        return [COMPONENT_REGISTRY]

    def config_component_layout(self) -> list[Any]:
        svc = self.service_type.value
        # Geen ``depends_on`` op de dienstenlijst: de keuze IS de selectie, dus het veld
        # zou wachten op wat het zelf zet.
        return [
            Div(
                slot=COMPONENT_IMAGE_SLOT,
                children=[f"services{{{svc}}}/config/registry"],
            )
        ]

    def config_deployment_component_visualizers(self) -> list[EditableVisualizer]:
        return [DEPLOYMENT_COMPONENT_REGISTRY]

    def config_deployment_component_layout(self) -> list[Any]:
        svc = self.service_type.value
        # ``services`` is hier een dict keyed op dienstnaam, dus een gewoon padsegment. Het
        # fieldset staat er onvoorwaardelijk, want de dienstenlijst van het component is
        # geen veld van dit formulier.
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

    # --- het vrije label en de slug die eruit volgt -----------------------------------

    def generate_missing_values(self, project_data: dict[str, Any]) -> dict[str, str]:
        """Geef elke registry zonder ``name`` er een, afgeleid van zijn label.

        Hier en niet in een converter van de editable, want allebei de schrijfwegen komen
        hier langs: de portal via ``post_merge`` van de sectie en de API via
        ``registry.generate_missing_values``. Een bestaande naam blijft staan, ook als het
        label verandert.
        """
        # Lazy: ``opi.services.project`` leest ``opi.forms``, en dat leest via de providers
        # deze module.
        from opi.services.project import Project

        base = config_path(ConfigLayer.PROJECT, self.service_type, "config", "registries")
        registries = Project(project_data).get(base) or []
        if not isinstance(registries, list):
            return {}
        bezet = {entry["name"] for entry in registries if isinstance(entry, dict) and entry.get("name")}
        gegenereerd: dict[str, str] = {}
        for index, entry in enumerate(registries):
            if not isinstance(entry, dict) or entry.get("name") or not entry.get("display-name"):
                continue
            slug = registry_slug(str(entry["display-name"]), bezet)
            entry["name"] = slug
            bezet.add(slug)
            gegenereerd[f"{base}[{index}]/name"] = slug
        if gegenereerd:
            logger.info(
                f"Registrynaam afgeleid voor {len(gegenereerd)} registry(s) van project "
                f"'{project_data.get('name', 'unknown')}'"
            )
        return gegenereerd

    def _generate_missing_names(self, project_data: dict[str, Any], _form_data: dict[str, Any]) -> None:
        """De ``post_merge``-vorm van :meth:`generate_missing_values`: de portal geeft twee
        dicts en wil niets terug, en een implementatie bedient allebei de wegen."""
        self.generate_missing_values(project_data)

    # --- projectniveau: de wizardsectie ----------------------------------------------

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return super().config_form_section(layer)
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            cached = FormSection(
                section_id=self.config_section_id or "image-registries-config",
                title="Eigen container registries",
                icon="server",
                description="De private registries waaruit dit project images haalt",
                visible=self._config_selected,
                post_save_action="process_project",
                editables=[REGISTRIES_SEQUENCE],
                layout=[config_path(ConfigLayer.PROJECT, self.service_type, "config", "registries")],
                enforcer=RegistryTokenEnforcer(),
                post_merge=self._generate_missing_names,
            )
            self._config_section_cache = cached
        return cached

    # --- de projectpagina ---------------------------------------------------------

    @on(UIEvent.PROJECT_SECTIONS)
    def registries_block(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
        """Wat dit project aan eigen registries heeft.

        De toestand van de proxy staat in het cluster en wordt lazy opgehaald (``web.py``).
        """
        registries = project_registries(ctx.project_data)
        if not registries:
            return []
        # Alleen wat het sjabloon toont. ``ctx.project_data`` is ONTSLEUTELD, dus de entry
        # zelf draagt het token in platte tekst; dat hoort niet in een rendercontext. Van het
        # token gaat daarom alleen de VRAAG mee of het er is: sinds de gebruikersnaam optioneel
        # is (RC-187) is dat het enige waaraan het blok een entry met inloggegevens herkent.
        getoond = [
            {
                **{key: registry.get(key) for key in ("name", "upstream", "username", "secretName")},
                "has_token": bool(registry.get("password")),
            }
            for registry in registries
        ]
        return [
            DetailPageSection(
                template="image_registries/section-detail.html.j2",
                context={"registries": getoond, "project_name": ctx.project_data.get("name", "")},
            )
        ]

    def web_routers(self) -> list[Any]:
        # Lazy: de router leest de projectstore, en die leest via ``project_validation`` en
        # ``opi.forms`` deze module.
        from opi.services.catalog.image_registries.web import image_registries_router

        return [*super().web_routers(), image_registries_router]

    # --- regels over het hele project --------------------------------------------------

    def validate_project(self, project_data: dict[str, Any]) -> list[str]:
        """De drie eigendomsregels rond de proxy-organisaties (zie ``ownership.py``), plus
        de weg terug (``references.py``).

        De eerste drie kijken naar de andere projecten op het cluster en niet naar een
        configblok, dus ze kunnen niet in ``validate_config``. Draaien ook zonder dat dit
        project de dienst aanvinkt: het gaat om waar een image NAAR wijst. De vierde legt
        componenten naast de registrylijst en hoort om dezelfde reden hier.
        """
        return [
            *validate_proxy_organization_ownership(project_data),
            *validate_registry_entry_ownership(project_data),
            *validate_proxy_organization_claims(project_data),
            *validate_registry_references(project_data),
        ]

    # --- projectbrede manifesten ------------------------------------------------------

    def contribute_project_manifests(self, ctx: ProjectManifestContext) -> list[ProjectManifestSpec]:
        """Wat deze dienst op het PROJECTniveau van de deployments-repo neerzet."""
        registries = project_registries(ctx.project_data)
        if not registries:
            return []
        backend = backend_for_cluster(ctx.cluster)
        specs: list[ProjectManifestSpec] = []
        seen: set[str] = set()
        for registry in registries:
            for spec in backend.manifests(ctx, registry):
                if spec.filename in seen:
                    # Twee registries op dezelfde bestandsnaam: de eerste wint, net als in
                    # de regellijst.
                    logger.warning(
                        f"Registry '{registry.get('name')}' van project '{ctx.project_name}' levert dezelfde "
                        f"projectmanifest '{spec.filename}' als een eerdere registry; de eerste blijft staan"
                    )
                    continue
                seen.add(spec.filename)
                specs.append(spec)
        return specs
