"""Editable-definities voor de dienst ``image-registries``.

Een regel die ook voor de API geldt staat in ``config_model.py``; dit bestand wijst er
via ``ModelFieldValidator`` naar.
"""

from __future__ import annotations

from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.forms.editables.validators import ModelFieldValidator, RequiredValidator
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.image_registries.config_model import (
    REGISTRY_NAME_MESSAGE,
    UPSTREAM_MESSAGE,
    RegistryEntry,
)
from opi.services.catalog.image_registries.converters import ProjectAgeSecretConverter
from opi.services.services_enums import ServiceType

_SVC = ServiceType.IMAGE_REGISTRIES


def _project(*parts: str) -> str:
    return config_path(ConfigLayer.PROJECT, _SVC, "config", *parts)


REGISTRY_NAME_EDITABLE = Editable(
    yaml_path=_project("registries[*]", "name"),
    validator=ModelFieldValidator(RegistryEntry, "name", REGISTRY_NAME_MESSAGE),
    required=True,
)

REGISTRY_UPSTREAM_EDITABLE = Editable(
    yaml_path=_project("registries[*]", "upstream"),
    required=True,
    validator=ModelFieldValidator(RegistryEntry, "upstream", UPSTREAM_MESSAGE),
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
        REGISTRY_NAME_EDITABLE,
        REGISTRY_UPSTREAM_EDITABLE,
        REGISTRY_USERNAME_EDITABLE,
        REGISTRY_PASSWORD_EDITABLE,
    ],
)

# ``values_must_exist``: een verwijzing naar een registry die niet bestaat sneuvelt bij
# het opslaan in plaats van pas bij het pullen.
COMPONENT_REGISTRY_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.COMPONENT, _SVC, "config", "registry"),
    values_provider="ImageRegistryOptionsProvider",
    values_must_exist=True,
    validator=RequiredValidator(),
    required=True,
    # "Verplicht" geldt alleen als het component de dienst aanvinkt.
    depends_on="components[*]/services",
    show_when={"contains": _SVC.value},
    virtualize=SERVICE_VIRTUALIZE,
)

DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.DEPLOYMENT_COMPONENT, _SVC, "config", "registry"),
    values_provider="ImageRegistryOptionsProvider",
    values_must_exist=True,
    # Geen depends_on: ``services`` is hier een dict, dus een "contains"-poort zegt niets.
    # Leeg laten betekent "volg het component".
    virtualize=SERVICE_VIRTUALIZE,
    remove_when_none=True,
)
