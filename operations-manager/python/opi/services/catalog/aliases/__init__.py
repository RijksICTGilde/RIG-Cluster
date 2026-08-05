"""aliases service package (RC-25).

A SYSTEM service, for the same reason as ``user-env-vars``: an alias map is part of what
a component is, not something a user switches on. See that package for the shape
argument; aliases differ from it in two ways.

They live on one layer only (the component), and their values are resolved *strictly*:
``substitute_variables`` raises on a reference it cannot resolve, where a user env-var
goes through the lenient ``substitute_known_variables`` because a dollar in a password is
not a typo. That difference is deliberate and is the reason aliases still exist next to
env-vars now that ``substitute_known_variables`` resolves ``$VAR`` in an env-var too; it
has to land somewhere before aliases can be folded into env-vars.

Like ``user-env-vars`` this service owns a plain component property
(``components[*]/aliases``) rather than a block in a ``services:`` list, declared through
``owned_property``. No project file changes.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.aliases.config_model import AliasesConfig
from opi.services.catalog.base import ConfigLayer, Service
from opi.services.services_enums import ServiceType


class AliasesService(Service):
    service_type = ServiceType.ALIASES
    config_model = AliasesConfig
    config_schema_version = "1.0"
    owned_property = "aliases"
    # Directly above user-env-vars, matching the order of the hand-authored "Variabelen"
    # fieldset this replaces.
    config_component_order = 5

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        # An open map (a RootModel keyed by alias name), so there is no fixed field list.
        return []

    def config_editables(self, layer: ConfigLayer):
        from opi.services.catalog.aliases.editables import COMPONENT_ALIASES_EDITABLE

        return [COMPONENT_ALIASES_EDITABLE] if layer is ConfigLayer.COMPONENT else []

    def config_component_visualizers(self):
        from opi.services.catalog.aliases.visualizers import COMPONENT_ALIASES

        return [COMPONENT_ALIASES]

    def config_component_layout(self) -> list[Any]:
        from opi.forms.layout import Fieldset

        return [
            Fieldset(
                legend="Aliassen",
                description="Koppel platform-variabelen aan de namen die dit component verwacht.",
                children=["aliases"],
            )
        ]
