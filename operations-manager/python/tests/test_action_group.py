#!/usr/bin/env python3
"""
Test parsing of c-action-group with onclick attribute.
"""

from jinja_roos_components.components.registry import ComponentRegistry
from jinja_roos_components.html_parser import ComponentHTMLParser

# The problematic HTML
html = """
<c-action-group
    :actions="[
        {
            'type': 'button',
            'kind': 'tertiary',
            'size': 'md',
            'label': 'Annuleren',
            'onclick': 'window.location.href="/dashboard"'
        }
    ]" />
"""

print("Testing c-action-group parsing...")
print("=" * 60)
print("Input HTML:")
print(html)
print("=" * 60)

# Parse
parser = ComponentHTMLParser(ComponentRegistry())
components = parser.parse_components(html)

print(f"\nFound {len(components)} components")
if components:
    comp = components[0]
    print(f"Component: {comp['tag']}")
    print(f"Available keys: {comp.keys()}")
    print(f"Attributes (key 'attributes'): {comp.get('attributes', {})}")
    print(f"Attributes (key 'attrs'): {comp.get('attrs', {})}")
    print(f"Self-closing: {comp.get('self_closing', False)}")

    # Check the attributes in detail
    for key, value in comp.get("attributes", {}).items():
        print(f"\nAttribute '{key}':")
        print(f"  Type: {type(value)}")
        print(f"  Value: {value}")
        if '"' in value:
            print("  Contains quotes: Yes")
            print(f"  Raw repr: {value!r}")
