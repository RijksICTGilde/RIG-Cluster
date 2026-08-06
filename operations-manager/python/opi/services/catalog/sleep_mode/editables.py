"""Editable definitions for the sleep-mode service (project-level config).

Almost every field is a selection with a value provider (wake mode, the two TTL
deadlines, the waker component and the two Ja/Nee booleans); only ``match`` is free
text, because it holds glob patterns for future deployment names (e.g. ``PR-*``).
"""

from __future__ import annotations

from opi.forms.editables.converters import BooleanConverter, CommaSeparatedListConverter, EmptyToNoneConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services_enums import ServiceType

# Optional fields carry ``remove_when_none`` so an empty form field drops the key instead of
# writing ``null`` / ``''`` / ``[]`` into the project file. Writing the model default out
# explicitly would freeze it: a project saved today would keep the old value when the default
# changes later. Deliberately NOT on ``enabled`` and ``waker``: those are booleans, the flag
# treats False as empty, and ``waker`` defaults to True, so unticking it would silently flip
# back to on.


def _path(*segments: str) -> str:
    return config_path(ConfigLayer.PROJECT, ServiceType.SLEEP_MODE, "config", *segments)


SLEEP_ENABLED_EDITABLE = Editable(
    yaml_path=_path("enabled"),
    values_provider="YesNoOptionsProvider",
    converter=BooleanConverter(),
    default="true",
    virtualize=SERVICE_VIRTUALIZE,
)

SLEEP_WAKE_MODE_EDITABLE = Editable(
    yaml_path=_path("wake-mode"),
    values_provider="WakeModeOptionsProvider",
    default="auto",
    virtualize=SERVICE_VIRTUALIZE,
)

SLEEP_MATCH_EDITABLE = Editable(
    yaml_path=_path("match"),
    converter=CommaSeparatedListConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

SLEEP_AFTER_DEPLOY_EDITABLE = Editable(
    yaml_path=_path("sleep-after-deploy"),
    values_provider="SleepAfterDeployOptionsProvider",
    default="48h",
    virtualize=SERVICE_VIRTUALIZE,
)

SLEEP_AFTER_WAKE_EDITABLE = Editable(
    yaml_path=_path("sleep-after-wake"),
    values_provider="SleepAfterWakeOptionsProvider",
    default="1h",
    virtualize=SERVICE_VIRTUALIZE,
)

SLEEP_WAKER_EDITABLE = Editable(
    yaml_path=_path("waker"),
    values_provider="YesNoOptionsProvider",
    converter=BooleanConverter(),
    default="true",
    virtualize=SERVICE_VIRTUALIZE,
)

SLEEP_WAKER_COMPONENT_EDITABLE = Editable(
    yaml_path=_path("waker-component"),
    values_provider="WakerComponentOptionsProvider",
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

SLEEP_TITLE_EDITABLE = Editable(
    yaml_path=_path("title"),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

SLEEP_DESCRIPTION_EDITABLE = Editable(
    yaml_path=_path("description"),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

SLEEP_MODE_EDITABLES = [
    SLEEP_ENABLED_EDITABLE,
    SLEEP_WAKE_MODE_EDITABLE,
    SLEEP_MATCH_EDITABLE,
    SLEEP_AFTER_DEPLOY_EDITABLE,
    SLEEP_AFTER_WAKE_EDITABLE,
    SLEEP_WAKER_EDITABLE,
    SLEEP_WAKER_COMPONENT_EDITABLE,
    SLEEP_TITLE_EDITABLE,
    SLEEP_DESCRIPTION_EDITABLE,
]
