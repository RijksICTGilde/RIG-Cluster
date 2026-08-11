"""
Widget adapters for form rendering.

This package provides abstract and concrete widget adapters
that translate FormField instances into HTML for specific UI frameworks.
"""

from opi.forms.widgets.base import WidgetAdapter
from opi.forms.widgets.fields import FieldWidgetAdapter

__all__ = ["FieldWidgetAdapter", "WidgetAdapter"]

# LOTCWidgetAdapter staat er BEWUST niet bij: die trekt de templateomgeving mee, en die
# leunt via de dienstenregistry weer op opi.forms. Importeer hem uit
# opi.forms.widgets.lotc.
