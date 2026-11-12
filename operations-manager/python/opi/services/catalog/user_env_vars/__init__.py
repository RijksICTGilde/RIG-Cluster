"""user-env-vars service package.

A SYSTEM service (``kind=SYSTEM``): every component has environment variables, so a user
should never have to switch them on and they never appear in the service picker. Being a
service gives them a typed config model, a committed schema fragment, a validator on the
way in, and a declared form section at each layer they live on.

They fit the service shape exactly. They exist on two layers with a merge between them
(the deployment-component value wins over the component value, see
``ProjectManager``), they carry AGE-encrypted values, and they need validation
(``KEY=value`` or YAML, with valid environment-variable names).

The one thing that is NOT service-shaped is where the data sits: this service owns the
plain ``user-env-vars`` property of a component, not a block inside a ``services:`` list.
``owned_property`` declares that, so the generic config validation walks it.

The "declared form section" above has an exception: the values may hold secrets and do not
belong in a general form or on a display page. The only place the portal shows and writes
them is a window of its own behind a fresh step-up (``opi/web/router_env_vars.py``); the
layers are therefore in ``form_exempt_layers``. The API read keeps masking the values
(``owned_value_is_secret``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from opi.services.catalog.base import ConfigLayer, Service
from opi.services.catalog.user_env_vars.config_model import UserEnvVarsConfig
from opi.services.services import ServiceDefinition
from opi.services.services_enums import ServiceKind, ServiceType

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable


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
        color="donkergeel",
        variables=[],
        # Always present, never in the project file's services list -> a system
        # service (kind=SYSTEM also keeps it out of the picker).
        kind=ServiceKind.SYSTEM,
    )
    config_model = UserEnvVarsConfig
    config_schema_version = "1.0"
    # May enrol itself: a system service whose values are a property of the component, so
    # there is no project-level decision to make first.
    allows_implicit_project_selection = True
    owned_property = "user-env-vars"
    # One AGE block for the whole set: the plaintext is the KEY=value text the form
    # posts and the deploy path parses, so it can only be encrypted as a whole.
    owned_values_map = True

    form_exempt_layers: ClassVar[dict[ConfigLayer, str]] = {
        ConfigLayer.COMPONENT: (
            "De waarden kunnen geheimen bevatten en horen niet in een algemeen bewerk"
            "formulier; het eigen venster achter een verse step-up (RC-388) is de enige "
            "bewerkweg. Alleen de aanmaakwizard kent het veld: schrijven zonder dat er "
            "een waarde was om te lezen."
        ),
        ConfigLayer.DEPLOYMENT_COMPONENT: (
            "Dezelfde reden als op de componentlaag: alleen het step-up-venster (RC-388) "
            "leest en schrijft deze overschrijvingen."
        ),
    }
    # Ordering among the component services; this service supplies no form fields (see
    # form_exempt_layers), so the place only matters for the registration.
    config_component_order = 6

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        # The config IS the value (a RootModel), so there are no named fields to accept.
        return []

    def config_editables(self, layer: ConfigLayer) -> list[Editable]:
        from opi.services.catalog.user_env_vars.editables import (
            COMPONENT_USER_ENV_VARS_EDITABLE,
            DEPLOYMENT_COMP_USER_ENV_VARS_EDITABLE,
        )

        if layer is ConfigLayer.COMPONENT:
            return [COMPONENT_USER_ENV_VARS_EDITABLE]
        if layer is ConfigLayer.DEPLOYMENT_COMPONENT:
            return [DEPLOYMENT_COMP_USER_ENV_VARS_EDITABLE]
        return []
