"""Editable-definities voor de dienst ``image-registries``.

Twee lagen: de registries op projectniveau (een sequence), en de verwijzing bij naam op
component- en deployment-componentniveau (een select).

Hier staan het yaml-pad, de validators en de converters, en verder niets: het formulier en
de API lopen daarmee hetzelfde logicapad. Waar een regel ook voor de API geldt staat hij in
``config_model.py`` en wijst dit bestand ernaar (``ModelFieldValidator``), zodat er een
definitie is en geen tweeling die uit elkaar loopt.
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


# Ook deze regel staat in het MODEL, en om dezelfde reden als bij ``upstream``: het
# formulier mag niet iets anders toelaten dan de API en dan een met de hand geschreven
# projectbestand.
REGISTRY_NAME_EDITABLE = Editable(
    yaml_path=_project("registries[*]", "name"),
    validator=ModelFieldValidator(RegistryEntry, "name", REGISTRY_NAME_MESSAGE),
    required=True,
)

# De regel staat in het MODEL en niet hier. Dit veld gaat een manifest in en het is de
# sleutel waarop een image wordt herkend, dus het formulier mag niet iets anders toelaten
# dan de API en dan een met de hand geschreven projectbestand. ModelFieldValidator wijst
# naar dezelfde constraint waarmee ``validate_service_configs`` een opgeslagen bestand
# toetst; alleen de UITLEG komt van hier, want de pydantic-melding is Engels en praat over
# patronen.
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

# De keuze bij een component komt van de dienst, niet uit het componentformulier: een
# gewone dienstvermelding met een configblok, in dezelfde vorm die publish-on-web en
# temp-storage daar al gebruiken. values_must_exist zorgt dat een verwijzing naar een
# registry die niet bestaat bij het OPSLAAN sneuvelt in plaats van pas bij het pullen.
COMPONENT_REGISTRY_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.COMPONENT, _SVC, "config", "registry"),
    values_provider="ImageRegistryOptionsProvider",
    values_must_exist=True,
    validator=RequiredValidator(),
    required=True,
    # Alleen als het component de dienst aanvinkt. Zonder deze poort zou "verplicht" ook
    # gelden voor elk component dat een publieke image draait, en dat is precies de
    # non-waarde die er niet hoort te zijn: geen vermelding, geen sleutel.
    depends_on="components[*]/services",
    show_when={"contains": _SVC.value},
    virtualize=SERVICE_VIRTUALIZE,
)

DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.DEPLOYMENT_COMPONENT, _SVC, "config", "registry"),
    values_provider="ImageRegistryOptionsProvider",
    values_must_exist=True,
    # Geen depends_on: op een deployment-component is ``services`` een DICT keyed op
    # dienstnaam, dus een "contains"-poort op die lijst zegt daar niets. Het veld is
    # optioneel en leeg laten betekent "volg het component" (remove_when_none).
    virtualize=SERVICE_VIRTUALIZE,
    remove_when_none=True,
)
