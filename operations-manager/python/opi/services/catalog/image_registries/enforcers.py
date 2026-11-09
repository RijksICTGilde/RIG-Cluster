"""The token check at save time.

On a pull, a token that is too narrow yields ``ImagePullBackOff`` with "repository not found",
a message that points the wrong way. What is checked is the tag listing of a repository this
project really pulls an image from; when there is none yet, only whether the registry accepts
the token.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.connectors.skopeo import REFUSED_DESTINATION_REASON, UNREADABLE_REASON, SkopeoConnector
from opi.forms.editables.enforcers import FieldError
from opi.forms.editables.service_path import smart_get_value
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.image_registries.naming import PULL_USERNAME_PLACEHOLDER
from opi.services.catalog.image_registries.rules import normalize_image, normalize_prefix
from opi.services.services_enums import ServiceType
from opi.utils.age import carries_encrypted_value, decrypt_password_smart_sync, project_private_key_or_none
from opi.utils.naming import upstream_host

logger = logging.getLogger(__name__)

_REGISTRIES_PATH = config_path(ConfigLayer.PROJECT, ServiceType.IMAGE_REGISTRIES, "config", "registries")


class RegistryTokenEnforcer:
    """Checks per registry whether the credentials may read a repository of this project."""

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
            given_username = registry.get("username")
            password = _plain_token(registry, value)
            if not upstream or not password:
                # An entry with a secretName has no token to check; an entry with neither is
                # refused by the model (``RegistryEntry``) on save.
                continue
            # The same placeholder the backend puts in the dockerconfigjson, because this is
            # the check ON that pair: with a different username we would measure something
            # other than what gets written.
            username = str(given_username) if given_username else PULL_USERNAME_PLACEHOLDER
            repository = _repository_under(str(upstream), images)
            if repository is None:
                # No image yet (the create wizard asks for the registry before the
                # components): check that the registry accepts the token at all, so a
                # wrong or expired token shows on this step instead of at the end.
                ok, reason = await connector.check_login(str(upstream), username, str(password))
                message = _login_refused_message(upstream_host(str(upstream)), bool(given_username))
            else:
                ok, reason = await connector.check_repository_access(repository, username, str(password))
                message = _access_denied_message(repository, bool(given_username))
            if not ok and reason == REFUSED_DESTINATION_REASON:
                raise FieldError(f"{_REGISTRIES_PATH}[{index}]/upstream", REFUSED_DESTINATION_REASON.capitalize())
            if not ok:
                raise FieldError(f"{_REGISTRIES_PATH}[{index}]/password", message)
        return value


def _access_denied_message(repository: str, has_username: bool) -> str:
    """What the consumer sees when the registry does not let us in.

    The reason is fixed and does not come from skopeo (see ``UNREADABLE_REASON``). When the
    check fails while the optional username was empty, that is the likely cause (Docker Hub,
    Quay), and the message says so.
    """
    if has_username:
        opening = f"Met deze gebruikersnaam en dit token kunnen we '{repository}' niet lezen: {UNREADABLE_REASON}."
        advice = "Het token heeft leesrecht op packages nodig."
    else:
        opening = f"Met dit token kunnen we '{repository}' niet lezen: {UNREADABLE_REASON}."
        advice = (
            "Het token heeft leesrecht op packages nodig. Je hebt geen gebruikersnaam ingevuld: "
            "bij Docker Hub en Quay is die wel nodig, dus vul daar je accountnaam of de naam van "
            "je robotaccount in."
        )
    return f"{opening} {advice}"


def _login_refused_message(host: str, has_username: bool) -> str:
    """What the user sees when the registry does not accept the token at all."""
    opening = f"{host} accepteert dit token niet: {UNREADABLE_REASON}."
    advice = "Controleer of je het hele token hebt geplakt en of het nog geldig is."
    if not has_username:
        advice += " Bij Docker Hub en Quay is ook een gebruikersnaam nodig."
    return f"{opening} {advice}"


def _connector() -> SkopeoConnector:
    """The skopeo connector; a service never talks to the outside world itself.

    Without a safety net: when skopeo is missing, ``check_repository_access`` returns ok
    itself.
    """
    return SkopeoConnector()


def _plain_token(registry: dict[str, Any], project_data: dict[str, Any]) -> str | None:
    """The token in readable form, or None when there is nothing to check.

    The enforcer receives the STORED shape, so what the converter made of it; handing that to
    skopeo would reject a valid token. Unlike in ``backends.py`` an unusable key is not an
    error here but a reason to stay quiet: this is a check.
    """
    stored = registry.get("password")
    if not isinstance(stored, str) or not stored:
        return None
    private_key = project_private_key_or_none(project_data) if carries_encrypted_value(stored) else None
    try:
        return decrypt_password_smart_sync(stored, private_key)
    except ValueError:
        logger.warning(f"Registry '{registry.get('name')}': token niet uit te pakken; het token wordt niet getoetst")
        return None


def _project_images(data: dict[str, Any]) -> list[str]:
    """Every image this project names anywhere, normalised."""

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
    """The first repository among these images that falls under ``upstream``, without tag."""
    prefix = normalize_prefix(upstream)
    for image in images:
        if image == prefix or image.startswith(prefix + "/"):
            return _strip_reference(image)
    return None


def _strip_reference(image: str) -> str:
    """``host/path/app:tag`` and ``host/path/app@sha256:..`` -> ``host/path/app``."""
    reference = image.split("@", 1)[0]
    repository, separator, tag = reference.rpartition(":")
    if not separator or "/" in tag:
        return reference
    return repository
