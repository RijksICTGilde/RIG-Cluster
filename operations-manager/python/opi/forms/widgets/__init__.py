"""
Widget adapters for form rendering.

This package provides abstract and concrete widget adapters
that translate FormField instances into HTML for specific UI frameworks.
"""

from opi.forms.widgets.base import WidgetAdapter
from opi.forms.widgets.roos import ROOSWidgetAdapter

__all__ = ["ROOSWidgetAdapter", "WidgetAdapter"]
