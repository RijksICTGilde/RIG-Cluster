"""Editable-definities voor de dienst ``image-registries``.

Twee lagen: de registries op projectniveau (een sequence), en de verwijzing bij naam op
component- en deployment-componentniveau (een select).
"""

from __future__ import annotations

from typing import Any

from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.forms.editables.validators import KubernetesNameValidator, RequiredValidator
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.image_registries.converters import ProjectAgeSecretConverter
from opi.services.services_enums import ServiceType

_SVC = ServiceType.IMAGE_REGISTRIES


class UpstreamValidator:
    """De registry waar de images echt staan: host, eventueel gevolgd door een pad.

    De afnemer schrijft de UPSTREAM, nooit een RCR-URL. Dat houdt het bestand
    overdraagbaar naar een ander platform en het is precies wat er vandaag met de publieke
    proxies ook gebeurt. Een protocol hoort er niet in, want een image-verwijzing draagt er
    geen, en een tag of digest hoort bij de image en niet bij de registry.
    """

    def validate(self, value: Any, context: dict[str, Any] | None = None) -> list[str]:
        if not value:
            return ["Dit veld is verplicht"]
        if not isinstance(value, str):
            return ["Vul de registry in als tekst, bijvoorbeeld code.overheid.nl/jouw-naam"]
        text = value.strip()
        if "://" in text:
            return ["Laat het protocol weg: schrijf code.overheid.nl/jouw-naam, niet https://..."]
        host = text.split("/", 1)[0]
        if "." not in host and host != "localhost" and ":" not in host:
            return ["De registry moet met een hostnaam beginnen, bijvoorbeeld code.overheid.nl/jouw-naam"]
        path = text.split("/", 1)[1] if "/" in text else ""
        if "@" in text or ":" in path:
            return ["Laat de tag of digest weg: die hoort bij de image, niet bij de registry"]
        if text != text.lower():
            return ["Schrijf de registry in kleine letters"]
        return []


def _project(*parts: str) -> str:
    return config_path(ConfigLayer.PROJECT, _SVC, "config", *parts)


REGISTRY_NAME_EDITABLE = Editable(
    yaml_path=_project("registries[*]", "name"),
    validator=KubernetesNameValidator("Registrynaam"),
    required=True,
)

REGISTRY_UPSTREAM_EDITABLE = Editable(
    yaml_path=_project("registries[*]", "upstream"),
    required=True,
    validator=UpstreamValidator(),
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
    depends_on="deployments[*]/components[*]/services",
    show_when={"contains": _SVC.value},
    virtualize=SERVICE_VIRTUALIZE,
    remove_when_none=True,
)
