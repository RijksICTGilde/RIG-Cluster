"""De dienst ``image-registries``.

Bewust naar het ONDERWERP genoemd in plaats van naar de functie: niet "private
registries", want de dienst regelt ook wat er met publieke images gebeurt, en niet
"repositories", want Quay gebruikt dat woord voor de dingen ín een registry.

De dienst kent twee bronnen van waarheid en één bewerking:

* de **clusterconfiguratie** (``get_image_registries_config``): welke provisioning-backend
  hier geldt, de platformfeiten, en de tabel van upstream naar gedeelde proxy;
* de **gebruikersconfiguratie**: de private registries die dit project zelf opgeeft, plus
  de verwijzing per component;
* en ``resolve_image()`` (``resolution.py``), die beide als regels achter elkaar zet en de
  eerste match toepast.

Drie dingen houden netjes dat een ``USER``-dienst een tabel draagt die ook geldt voor
projecten die hem nooit aanraken:

1. ``resolve_image()`` is een FUNCTIE, geen haak -- geen staat, geen activatie.
2. Wat wél activatie kent is alleen het PROVISIONEREN, en dat is door data gedreven: geen
   registries in de config betekent geen ``Organization``, geen secret, geen bijdrage aan
   het projectniveau.
3. De selectie wordt AFGELEID uit de data: de dienst staat aan zodra er minstens één
   registry in staat. Dan bestaat "aangevinkt maar leeg" niet en wordt de clustertabel
   nergens als een keuze van de afnemer gepresenteerd.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.services.catalog.base import (
    ConfigLayer,
    ProjectManifestContext,
    ProjectManifestSpec,
    Service,
    config_path,
)
from opi.services.catalog.image_registries.config_model import ComponentRegistryConfig, ImageRegistriesConfig
from opi.services.catalog.image_registries.resolution import project_registries
from opi.services.services import ServiceDefinition, service_entry_name
from opi.services.services_enums import ServiceBinding, ServiceType

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
    # De registries staan op projectniveau, dus een component dat de dienst aanvinkt mag
    # zichzelf op projectniveau bijschrijven; de lege lijst die dat oplevert is precies
    # "aangevinkt, nog niets ingevuld" en het formulier vraagt er meteen om.
    allows_implicit_project_selection = True
    config_component_order = 8

    def config_model_for(self, layer: ConfigLayer) -> type[BaseModel] | None:
        # De registries op het project, de verwijzing bij naam op (deployment-)component.
        if layer in (ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT_COMPONENT):
            return ComponentRegistryConfig
        return self.config_model

    def config_roles(self, layer: ConfigLayer):
        from opi.services.catalog.base import ConfigRole

        # Het project DEFINIEERT geen catalogus onder ``data``: de registries staan onder
        # ``config`` en worden door het project zelf gebruikt. Een component GEBRUIKT er
        # een en BINDT hem daarmee aan zijn image -- dezelfde vermelding doet allebei.
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
        from opi.services.catalog.image_registries.editables import (
            COMPONENT_REGISTRY_EDITABLE,
            DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE,
            REGISTRIES_SEQUENCE_EDITABLE,
        )

        if layer is ConfigLayer.PROJECT:
            return [REGISTRIES_SEQUENCE_EDITABLE]
        if layer is ConfigLayer.COMPONENT:
            return [COMPONENT_REGISTRY_EDITABLE]
        if layer is ConfigLayer.DEPLOYMENT_COMPONENT:
            return [DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE]
        return []

    # --- component- en deployment-componentniveau: volledig automatisch ---------------
    # Drie haken en klaar, precies zoals instructions/services.md het noemt. Geen
    # ``owned_property``, geen uitbreiding aan het formulierraamwerk, en geen hardgecodeerd
    # veld naast ``image:`` in het componentformulier.

    def config_component_visualizers(self) -> list[EditableVisualizer]:
        from opi.services.catalog.image_registries.visualizers import COMPONENT_REGISTRY

        return [COMPONENT_REGISTRY]

    def config_component_layout(self) -> list[Any]:
        from opi.forms.layout import Fieldset

        svc = self.service_type.value
        return [
            Fieldset(
                legend="Eigen registry",
                depends_on="services",
                show_when={"contains": svc},
                children=[f"services{{{svc}}}/config/registry"],
            )
        ]

    def config_deployment_component_visualizers(self) -> list[EditableVisualizer]:
        from opi.services.catalog.image_registries.visualizers import DEPLOYMENT_COMPONENT_REGISTRY

        return [DEPLOYMENT_COMPONENT_REGISTRY]

    def config_deployment_component_layout(self) -> list[Any]:
        from opi.forms.layout import Fieldset

        svc = self.service_type.value
        return [
            Fieldset(
                legend="Eigen registry",
                depends_on="services",
                show_when={"contains": svc},
                children=[f"services{{{svc}}}/config/registry"],
            )
        ]

    # --- projectniveau: de wizardsectie ----------------------------------------------

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            # De component- en deployment-componentsectie bouwt de basisklasse zelf uit de
            # visualizers en layout-nodes die hierboven al gedeclareerd staan.
            return super().config_form_section(layer)
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.image_registries.visualizers import REGISTRIES_SEQUENCE

            cached = FormSection(
                section_id=self.config_section_id or "image-registries-config",
                title="Eigen container registries",
                icon="server",
                description="De private registries waaruit dit project images haalt",
                visible=self._config_selected,
                post_save_action="process_project",
                editables=[REGISTRIES_SEQUENCE],
                layout=[config_path(ConfigLayer.PROJECT, self.service_type, "config", "registries")],
            )
            self._config_section_cache = cached
        return cached

    # --- projectbrede manifesten ------------------------------------------------------

    def contribute_project_manifests(self, ctx: ProjectManifestContext) -> list[ProjectManifestSpec]:
        """Wat deze dienst op het PROJECTniveau van de deployments-repo neerzet.

        Door data gedreven: geen registries in de config betekent geen enkel bestand, dus
        ook geen bijdrage aan het projectniveau. Welke vorm een registry aanneemt is de
        keuze van de backend van het cluster, niet van deze methode.
        """
        from opi.services.catalog.image_registries.backends import backend_for_cluster

        registries = project_registries(ctx.project_data)
        if not registries:
            return []
        backend = backend_for_cluster(ctx.cluster)
        specs: list[ProjectManifestSpec] = []
        for registry in registries:
            specs.extend(backend.manifests(ctx, registry))
        return specs
