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
from opi.services.catalog.aliases.references import is_reference, validate_alias_value
from opi.services.catalog.base import ConfigLayer, Service
from opi.services.services import ServiceDefinition
from opi.services.services_enums import ServiceBinding, ServiceKind, ServiceType


class AliasesService(Service):
    service_type = ServiceType.ALIASES
    definition = ServiceDefinition(
        name="Aliassen",
        description=(
            "Systeemdienst: koppelt platform-variabelen aan de namen die een component "
            "verwacht (POSTGRES_HOST=$DATABASE_SERVER_HOST). Draait altijd, is niet "
            "kiesbaar. Een onbekende verwijzing is hier een harde fout, anders dan bij "
            "een eigen omgevingsvariabele."
        ),
        help_template="aliases/help.md",
        icon="instellingen",
        color="grijs-600",
        binding=ServiceBinding.COMPONENT,
        variables=[],
        kind=ServiceKind.SYSTEM,
    )
    config_model = AliasesConfig
    config_schema_version = "1.0"
    # May enrol itself (RC-84): a system service whose values are a property of the
    # component, so there is no project-level decision to make first.
    allows_implicit_project_selection = True
    owned_property = "aliases"
    # One AGE block for the whole set, exactly like user-env-vars (RC-106). Stored per
    # value before that, which made every reader depend on a decrypt step of its own.
    owned_values_map = True
    # Directly above user-env-vars, matching the order of the hand-authored "Variabelen"
    # fieldset this replaces.
    config_component_order = 5

    def validate_owned_value(self, key: str, value: str) -> None:
        # The promise in the description ("Een onbekende verwijzing is hier een harde
        # fout") now holds at the moment of writing instead of only at deploy time.
        validate_alias_value(key, value)

    def owned_value_is_secret(self, key: str, value: str) -> bool:
        # A reference is the coupling itself, not a secret; anything else may be one.
        return not is_reference(value)

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
