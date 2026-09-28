"""Build a form field out of a service's declared room (RC-168, step 4).

A field whose bounds the service already declared should not have them typed out a
second time next to its widget. ``setting_field`` turns one ``ConfigSetting`` into the
``Editable`` + ``EditableVisualizer`` pair the wizard renders: the yaml path comes from
``config_path`` via the declaration, the input check from the same ``check`` the
project-file validation runs, the help text from ``latitude()`` and the prefill from the
service default.

That is the whole point of the mechanism: the screen and the save cannot show different
bounds, because there is only one declaration and both read it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opi.forms.editables.converters import IntegerConverter
from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.editables.validators import ConfigSettingValidator
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.config_settings import ChoiceSetting, ConfigSetting, IntegerSetting

if TYPE_CHECKING:
    from opi.services.catalog.base import ConfigLayer
    from opi.services.services_enums import ServiceType


def setting_field(
    setting: ConfigSetting,
    service: ServiceType,
    layer: ConfigLayer,
    *,
    widget: WidgetType | None = None,
    values_provider: str | None = None,
    virtualize: tuple[str, str] | None = ("services", "_services-config"),
    inherits: bool = False,
) -> EditableVisualizer:
    """The wizard field for ``setting`` at ``layer``, derived from the declaration.

    ``widget`` defaults to a number box for a whole number and a text box otherwise. A
    ``ChoiceSetting`` deserves a select, but a select needs an ``OptionsProvider``
    registered under a name, so pass ``widget=WidgetType.SELECT`` together with the
    ``values_provider`` that offers ``setting.allowed``; the validator here is the closed
    set either way, so the field is judged by the declaration whichever widget it wears.

    ``virtualize`` defaults to the services pair every per-service config field needs:
    without it the form posts over the service SELECTION list instead of next to it.

    ``inherits=True`` makes an empty field say nothing: no prefill and no stored key, so
    the value comes from a less specific layer or the service default. Without it an
    untouched field on a deployment writes the service default over the project's value.

    Raises:
        SettingError: if the service does not open the field up at ``layer``.
    """
    editable = Editable(
        yaml_path=setting.yaml_path(service, layer),
        validator=ConfigSettingValidator(setting),
        converter=IntegerConverter() if isinstance(setting, IntegerSetting) else None,
        default=None if inherits else setting.default,
        remove_when_none=inherits,
        values_provider=values_provider,
        virtualize=virtualize,
    )
    if widget is None:
        widget = WidgetType.NUMBER if isinstance(setting, IntegerSetting) else WidgetType.TEXT
    return EditableVisualizer(
        editable=editable,
        widget=widget,
        label=setting.label,
        description=setting.latitude(),
        examples=list(setting.allowed) if isinstance(setting, ChoiceSetting) else None,
    )
