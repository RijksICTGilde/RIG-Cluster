#!/usr/bin/env python3
"""Verify tertiary button generates correct classes."""

import sys
from pathlib import Path

# Add jinja-roos-components to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "jinja-roos-components"))

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import ComponentExtension

# Setup paths
component_templates = (
    Path(__file__).parent.parent.parent / "jinja-roos-components" / "jinja_roos_components" / "templates"
)

# Test tertiary button
test_template = """
<c-button kind="tertiary" size="md">Test Tertiary</c-button>
<c-button kind="tertiary" size="sm" :showIcon="'before'" :icon="'plus'">Gebruiker toevoegen</c-button>
"""

# Create environment
env = Environment(loader=FileSystemLoader([str(component_templates)]), extensions=[ComponentExtension], autoescape=True)

# Render template
template = env.from_string(test_template)
html_output = template.render()

print("Generated HTML for tertiary buttons:\n")
print(html_output)

# Check for expected classes
expected = 'class="utrecht-button utrecht-button--rvo-tertiary-action utrecht-button--rvo-md"'
if expected in html_output:
    print("\n✅ CONFIRMED: Tertiary button generates correct classes:")
    print('   class="utrecht-button utrecht-button--rvo-tertiary-action utrecht-button--rvo-md"')
else:
    print("\n❌ ERROR: Expected classes not found")
    sys.exit(1)

expected_sm = 'class="utrecht-button utrecht-button--rvo-tertiary-action utrecht-button--rvo-sm'
if expected_sm in html_output:
    print("\n✅ CONFIRMED: Tertiary button with size 'sm' also works correctly")
else:
    print("\n❌ ERROR: Small tertiary button classes not found")
    sys.exit(1)
