"""Editable definitions for the authorization-wall service (project-level banner)."""

from __future__ import annotations

from opi.forms.editables.converters import EmptyToNoneConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services_enums import ServiceType

AUTH_WALL_BANNER_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.AUTHORIZATION_WALL, "config", "banner"),
    # Optional free-text field: an empty submission leaves no key rather than writing
    # banner: "" or null. banner defaults to None in the model, not a bool, so
    # remove_when_none is safe here.
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

#: Signpost in the component form: the auth wall is switched on HERE and configured
#: ELSEWHERE.
#:
#: The service hangs off a component (that is where the oauth2-proxy joins the pod), while
#: its only setting, the banner text, is project-wide: one access page for the whole
#: project. The place that says so is the Authorization Wall card on the Services tab.
#:
#: A pointer and not a second editor: the field is project-wide, so a copy per component
#: would suggest it can be set per component.
#:
#: The path is never written: the visualizer is ``readonly``, and processing skips readonly
#: fields. Deliberately NOT ``transient``, however tempting that sounds for a field without
#: a value: ``transient`` together with ``depends_on`` already means something else in this
#: codebase (a field mirroring its parent, like the own domain), and the wizard tries to
#: fill such a field while redrawing, which fails on the wildcard in the path.
AUTH_WALL_COMPONENT_INFO_EDITABLE = Editable(
    yaml_path="components[*]/_authorization-wall-info",
    depends_on="components[*]/services",
    show_when={"contains": ServiceType.AUTHORIZATION_WALL.value},
)
