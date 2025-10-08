#!/usr/bin/env python3
"""
Test the _parse_attributes method directly.
"""

from jinja_roos_components.components.registry import ComponentRegistry
from jinja_roos_components.html_parser import ComponentHTMLParser

attrs_str = ''':actions="[
        {
            'type': 'button',
            'kind': 'tertiary',
            'size': 'md',
            'label': 'Annuleren',
            'onclick': 'window.location.href="/dashboard"'
        }
    ]"'''

print("Attributes string to parse:")
print(attrs_str)
print("\n" + "=" * 60)

# Create parser instance
parser = ComponentHTMLParser(ComponentRegistry())

# Call the method
result = parser._parse_attributes(attrs_str)

print("Parsed attributes:")
for key, value in result.items():
    print(f"  {key}: {value[:50]}..." if len(value) > 50 else f"  {key}: {value}")
