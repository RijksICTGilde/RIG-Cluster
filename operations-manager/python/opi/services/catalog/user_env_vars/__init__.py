"""user-env-vars service package (RC-25).

A SYSTEM service (``kind=SYSTEM``): every component has environment variables, so a user
should never have to switch them on and they never appear in the service picker. What
being a service buys them is everything the picker is not: a typed config model, a
committed schema fragment, a validator on the way in, and a declared form section at each
layer they live on.

They fit the service shape exactly. They exist on two layers with a merge between them
(the deployment-component value wins over the component value, see
``ProjectManager``), they carry AGE-encrypted values, and they need validation
(``KEY=value`` or YAML, with valid environment-variable names).

The one thing that is NOT service-shaped is where the data sits: this service owns the
plain ``user-env-vars`` property of a component, not a block inside a ``services:`` list.
``owned_property`` declares that, so the generic config validation walks it. Nothing in
any project file moves.

Together with ``aliases`` this is the first system service with a user interface --
``resource-tuning`` proved the headless half of ``ServiceKind.SYSTEM``.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.base import ConfigLayer, Service
from opi.services.catalog.user_env_vars.config_model import UserEnvVarsConfig
from opi.services.services import ServiceDefinition
from opi.services.services_enums import ServiceBinding, ServiceKind, ServiceType


class UserEnvVarsService(Service):
    service_type = ServiceType.USER_ENV_VARS
    definition = ServiceDefinition(
        name="Eigen omgevingsvariabelen",
        description=(
            "Systeemdienst: de eigen omgevingsvariabelen van een component, per component "
            "en per deployment-component. Draait altijd, is niet kiesbaar - elk component "
            "heeft ze. De waarden worden versleuteld opgeslagen."
        ),
        help_template="user_env_vars/help.md",
        icon="instellingen",
        color="grijs-600",
        binding=ServiceBinding.COMPONENT,
        variables=[],
        # Always present, never in the project file's services list -> a system
        # service (kind=SYSTEM also keeps it out of the picker).
        kind=ServiceKind.SYSTEM,
    )
    config_model = UserEnvVarsConfig
    config_schema_version = "1.0"
    # May enrol itself (RC-84): a system service whose values are a property of the
    # component, so there is no project-level decision to make first.
    allows_implicit_project_selection = True
    owned_property = "user-env-vars"
    # One AGE block for the whole set: the plaintext is the KEY=value text the form
    # posts and the deploy path parses, so it can only be encrypted as a whole.
    owned_values_map = True
    # Sits at the top of the per-component service fieldsets, where the hand-authored
    # "Variabelen" fieldset used to be, so the form order is unchanged.
    config_component_order = 6

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        # The config IS the value (a RootModel), so there are no named fields to accept;
        # the layers it lives on are declared through config_editables instead.
        return []

    def config_editables(self, layer: ConfigLayer):
        from opi.services.catalog.user_env_vars.editables import (
            COMPONENT_USER_ENV_VARS_EDITABLE,
            DEPLOYMENT_COMP_USER_ENV_VARS_EDITABLE,
        )

        if layer is ConfigLayer.COMPONENT:
            return [COMPONENT_USER_ENV_VARS_EDITABLE]
        if layer is ConfigLayer.DEPLOYMENT_COMPONENT:
            return [DEPLOYMENT_COMP_USER_ENV_VARS_EDITABLE]
        return []

    def config_component_visualizers(self):
        from opi.services.catalog.user_env_vars.visualizers import COMPONENT_USER_ENV_VARS

        return [COMPONENT_USER_ENV_VARS]

    def config_component_layout(self) -> list[Any]:
        from opi.forms.layout import Fieldset

        # No depends_on/show_when: a system service is always on, so its fieldset is
        # unconditional. That is the visible difference with a user service's fieldset.
        return [
            Fieldset(
                legend="Eigen omgevingsvariabelen",
                description="Omgevingsvariabelen voor dit component. Worden versleuteld opgeslagen.",
                children=["user-env-vars"],
            )
        ]

    def config_deployment_component_visualizers(self):
        from opi.services.catalog.user_env_vars.visualizers import DEPLOYMENT_COMP_USER_ENV_VARS

        return [DEPLOYMENT_COMP_USER_ENV_VARS]

    def config_deployment_component_layout(self) -> list[Any]:
        from opi.forms.layout import Fieldset

        return [
            Fieldset(
                legend="Omgevingsvariabelen",
                description="Deployment-specifieke omgevingsvariabelen voor dit component.",
                children=["user-env-vars"],
            )
        ]
