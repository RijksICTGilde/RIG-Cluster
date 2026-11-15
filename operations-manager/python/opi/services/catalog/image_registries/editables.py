"""Editable definitions for the ``image-registries`` service.

A rule that holds for the API too lives in ``config_model.py``; this file points at it
through ``ModelFieldValidator``.
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


# Not ``required``: a registry that carries only a slug must not become unsaveable. Name or
# label is a rule about the two fields together, in ``RegistryEntry``.
REGISTRY_DISPLAY_NAME_EDITABLE = Editable(
    yaml_path=_project("registries[*]", "display-name"),
    remove_when_none=True,
)

# The slug has to travel with the submission even though it is not on screen: the rows of a
# sequence are matched to their original row on ``name`` (``match_by_identity``), and without
# that key removing a row slides one registry's slug under another.
REGISTRY_NAME_EDITABLE = Editable(
    yaml_path=_project("registries[*]", "name"),
    validator=ModelFieldValidator(RegistryEntry, "name", REGISTRY_NAME_MESSAGE),
    remove_when_none=True,
)

# The ``default`` pre-fills the upstream from the images the project already has, and only
# when those are unambiguous. A default that overwrites nothing: it applies only to a row
# that carries no upstream yet, so to a new registry.
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

# No ``required`` and no ``depends_on`` on the services list: the choice IS the selection, so
# the field decides that list and not the other way round.
#
# ``values_must_exist``: a reference to a registry that does not exist fails on save instead
# of only on the pull.
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
    # No depends_on: ``services`` is a dict here, so a "contains" guard says nothing. Left
    # empty it means "follow the component".
    virtualize=SERVICE_VIRTUALIZE,
    remove_when_none=True,
)
