"""Widget adapters: they translate a ``FormField`` into HTML for one UI framework."""

from opi.forms.widgets.base import WidgetAdapter
from opi.forms.widgets.fields import FieldWidgetAdapter

__all__ = ["FieldWidgetAdapter", "WidgetAdapter"]

# LOTCWidgetAdapter is deliberately NOT exported here: it drags the template environment
# along, which leans back on opi.forms through the service registry. Import it from
# opi.forms.widgets.lotc.
