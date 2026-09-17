"""Editable-definities voor de dienst ``image-registries``.

Een regel die ook voor de API geldt staat in ``config_model.py``; dit bestand wijst er
via ``ModelFieldValidator`` naar.
"""

from __future__ import annotations

from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.forms.editables.validators import ModelFieldValidator
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.image_registries.config_model import (
    REGISTRY_NAME_MESSAGE,
    UPSTREAM_MESSAGE,
    RegistryEntry,
)
from opi.services.catalog.image_registries.converters import ProjectAgeSecretConverter, UpstreamConverter
from opi.services.catalog.image_registries.upstream import upstream_from_project_images
from opi.services.services_enums import ServiceType

_SVC = ServiceType.IMAGE_REGISTRIES


def _project(*parts: str) -> str:
    return config_path(ConfigLayer.PROJECT, _SVC, "config", *parts)


# Niet ``required``: een registry van voor RC-187 heeft alleen een slug en mag niet
# onopslaanbaar worden. Naam of label is een regel over de twee velden samen, in
# ``RegistryEntry``.
REGISTRY_DISPLAY_NAME_EDITABLE = Editable(
    yaml_path=_project("registries[*]", "display-name"),
    remove_when_none=True,
)

# De slug moet meekomen in de inzending, ook al staat hij niet op het scherm: de rijen van
# een reeks worden op ``name`` aan hun oorspronkelijke rij gekoppeld
# (``_match_original_item``), en zonder die sleutel schuift bij het weghalen van een rij de
# slug van de ene registry onder de andere.
REGISTRY_NAME_EDITABLE = Editable(
    yaml_path=_project("registries[*]", "name"),
    validator=ModelFieldValidator(RegistryEntry, "name", REGISTRY_NAME_MESSAGE),
    remove_when_none=True,
)

# De ``default`` vult de upstream vooruit in uit de images die het project al heeft, en
# alleen als die eenduidig zijn. Een default die niets overschrijft: hij geldt alleen voor
# een rij die nog geen upstream draagt, dus voor een nieuwe registry.
REGISTRY_UPSTREAM_EDITABLE = Editable(
    yaml_path=_project("registries[*]", "upstream"),
    required=True,
    validator=ModelFieldValidator(RegistryEntry, "upstream", UPSTREAM_MESSAGE),
    converter=UpstreamConverter(),
    default=upstream_from_project_images,
)

REGISTRY_USERNAME_EDITABLE = Editable(
    yaml_path=_project("registries[*]", "username"),
    remove_when_none=True,
)

REGISTRY_PASSWORD_EDITABLE = Editable(
    yaml_path=_project("registries[*]", "password"),
    converter=ProjectAgeSecretConverter(),
    remove_when_none=True,
)

REGISTRIES_SEQUENCE_EDITABLE = Editable(
    yaml_path=_project("registries"),
    virtualize=SERVICE_VIRTUALIZE,
    children=[
        REGISTRY_DISPLAY_NAME_EDITABLE,
        REGISTRY_NAME_EDITABLE,
        REGISTRY_UPSTREAM_EDITABLE,
        REGISTRY_USERNAME_EDITABLE,
        REGISTRY_PASSWORD_EDITABLE,
    ],
)

# Geen ``required`` en geen ``depends_on`` op de dienstenlijst: de keuze IS de selectie,
# dus het veld bepaalt die lijst en niet andersom.
#
# ``values_must_exist``: een verwijzing naar een registry die niet bestaat sneuvelt bij
# het opslaan in plaats van pas bij het pullen.
COMPONENT_REGISTRY_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.COMPONENT, _SVC, "config", "registry"),
    values_provider="ImageRegistryOptionsProvider",
    values_must_exist=True,
    hidden_without_options=True,
    virtualize=SERVICE_VIRTUALIZE,
    remove_when_none=True,
)

DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.DEPLOYMENT_COMPONENT, _SVC, "config", "registry"),
    values_provider="ImageRegistryOptionsProvider",
    values_must_exist=True,
    hidden_without_options=True,
    # Geen depends_on: ``services`` is hier een dict, dus een "contains"-poort zegt niets.
    # Leeg laten betekent "volg het component".
    virtualize=SERVICE_VIRTUALIZE,
    remove_when_none=True,
)
