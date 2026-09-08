"""De tokentoets bij het opslaan (D5).

Bij het pullen levert een te smal token ``ImagePullBackOff`` met "repository not found",
een melding die de verkeerde kant op wijst. Getoetst wordt het tag-overzicht van een
repository waar dit project werkelijk een image uit haalt; is die er niet, dan wordt er
niets geweigerd.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.connectors.skopeo import SkopeoConnector
from opi.forms.editables.converters import resolve_project_private_key
from opi.forms.editables.enforcers import FieldError
from opi.forms.editables.service_path import smart_get_value
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.image_registries.rules import normalize_image, normalize_prefix
from opi.services.services_enums import ServiceType
from opi.utils.age import carries_encrypted_value, decrypt_password_smart_sync

logger = logging.getLogger(__name__)

_REGISTRIES_PATH = config_path(ConfigLayer.PROJECT, ServiceType.IMAGE_REGISTRIES, "config", "registries")


class RegistryTokenEnforcer:
    """Toetst per registry of de inloggegevens een repository van dit project mogen lezen."""

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        registries = smart_get_value(value, _REGISTRIES_PATH) or []
        if not isinstance(registries, list):
            return value

        images = _project_images(value)
        connector = _connector()

        for index, registry in enumerate(registries):
            if not isinstance(registry, dict):
                continue
            upstream = registry.get("upstream")
            username = registry.get("username")
            password = _plain_token(registry, value)
            if not upstream or not username or not password:
                # Een publieke upstream zonder inloggegevens is geldige invoer.
                continue
            repository = _repository_under(str(upstream), images)
            if repository is None:
                logger.info(
                    f"Registry '{registry.get('name')}' heeft nog geen image in dit project; token niet getoetst"
                )
                continue
            ok, reason = await connector.check_repository_access(repository, str(username), str(password))
            if not ok:
                raise FieldError(
                    f"{_REGISTRIES_PATH}[{index}]/password",
                    f"Met deze gebruikersnaam en dit token kunnen we '{repository}' niet lezen. "
                    f"Het token heeft leesrecht op packages nodig. De registry zei: {reason}",
                )
        return value


def _connector() -> SkopeoConnector:
    """De skopeo-connector; een dienst praat nooit zelf met de buitenwereld.

    Zonder vangnet: ontbreekt skopeo, dan geeft ``check_repository_access`` zelf ok terug.
    """
    return SkopeoConnector()


def _plain_token(registry: dict[str, Any], project_data: dict[str, Any]) -> str | None:
    """Het token in leesbare vorm, of None als er niets te toetsen valt.

    De enforcer krijgt de OPGESLAGEN vorm, dus wat de converter ervan gemaakt heeft; die
    aan skopeo geven zou een geldig token afkeuren. Anders dan in ``backends.py`` is een
    onbruikbare sleutel hier geen fout maar een reden om te zwijgen: dit is een toets.
    """
    stored = registry.get("password")
    if not isinstance(stored, str) or not stored:
        return None
    private_key = resolve_project_private_key(project_data) if carries_encrypted_value(stored) else None
    try:
        return decrypt_password_smart_sync(stored, private_key)
    except ValueError:
        logger.warning(f"Registry '{registry.get('name')}': token niet uit te pakken; het token wordt niet getoetst")
        return None


def _project_images(data: dict[str, Any]) -> list[str]:
    """Elke image die dit project ergens noemt, genormaliseerd."""

    def images_of(components: Any) -> list[str]:
        return [
            normalize_image(component["image"])
            for component in components or []
            if isinstance(component, dict) and isinstance(component.get("image"), str) and component["image"]
        ]

    images = images_of(data.get("components"))
    for deployment in data.get("deployments", []) or []:
        if isinstance(deployment, dict):
            images.extend(images_of(deployment.get("components")))
    return images


def _repository_under(upstream: str, images: list[str]) -> str | None:
    """De eerste repository uit deze images die onder ``upstream`` valt, zonder tag."""
    prefix = normalize_prefix(upstream)
    for image in images:
        if image == prefix or image.startswith(prefix + "/"):
            return _strip_reference(image)
    return None


def _strip_reference(image: str) -> str:
    """``host/pad/app:tag`` en ``host/pad/app@sha256:..`` -> ``host/pad/app``."""
    reference = image.split("@", 1)[0]
    repository, separator, tag = reference.rpartition(":")
    if not separator or "/" in tag:
        return reference
    return repository
